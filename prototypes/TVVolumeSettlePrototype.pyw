"""Standalone relative TV-volume settling experiment (PySide6 only).

Copy SonyRemote.json beside this file. Close Sony Remote and other software
volume controllers before testing. No IPC, config writes, mute changes or restore.
"""
import ipaddress
import json
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QVBoxLayout, QWidget)

__version__ = '2026.10.24.1'
NO_PROGRESS_SECONDS = 5.0
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Cancelled(Exception):
    pass


def load_settings(folder):
    settings = json.loads((folder / 'SonyRemote.json').read_text(encoding='utf-8'))
    if not isinstance(settings, dict):
        raise ValueError('SonyRemote.json must contain a JSON object.')
    for key in ('Host', 'PreSharedKey'):
        if not isinstance(settings.get(key), str) or not settings[key].strip():
            raise ValueError('SonyRemote.json needs a non-empty ' + key + '.')
    return settings


def base_url(host):
    host = host.strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (any(char in host for char in '/:@?#') or any(char.isspace() for char in host)
                or all(label.isdigit() for label in host.split('.'))):
            raise ValueError('Host must be a valid IP address or hostname, without a URL or port.')
    else:
        if address.version == 6:
            host = '[' + host.replace('%', '%25') + ']'
    return 'http://' + host


class TvApi:
    def __init__(self, settings, log):
        self.url = base_url(settings['Host'])
        self.psk = settings['PreSharedKey']
        self.log = log
        self.request_id = 0

    def rpc(self, service, method, params=None):
        self.request_id += 1
        payload = dict(method=method, id=self.request_id,
                       params=[] if params is None else params, version='1.0')
        request = urllib.request.Request(self.url + '/sony/' + service,
            data=json.dumps(payload).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'X-Auth-PSK': self.psk})
        started = time.monotonic()
        self.log('HTTP START ' + service + '/' + method)
        try:
            with OPENER.open(request, timeout=2.0) as response:
                result = json.loads(response.read().decode('utf-8'))
            if not isinstance(result, dict):
                raise ValueError('TV response is not an object.')
            if 'error' in result:
                raise RuntimeError('TV rejected request: ' + repr(result['error']))
            if not isinstance(result.get('result'), list):
                raise ValueError('TV response has no result list.')
            return result['result']
        finally:
            self.log(f'HTTP END {service}/{method}: {(time.monotonic()-started)*1000:.1f} ms')

    def get_power(self):
        return self.rpc('system', 'getPowerStatus')[0]['status']

    def get_volume(self):
        for info in self.rpc('audio', 'getVolumeInformation')[0]:
            if info.get('target') == 'speaker':
                return info
        raise ValueError('TV did not report a speaker volume entry.')

    def adjust_volume(self, direction):
        result = self.rpc('audio', 'setAudioVolume',
            [{'target': 'speaker', 'volume': f'{direction:+d}'}])
        self.log('Relative command acknowledged: ' + json.dumps(result))


def integer(value, field):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.lstrip('+-').isdigit():
        return int(value)
    raise ValueError('TV ' + field + ' is not an integer.')


def read_volume(api, log, label, observed=None):
    info = api.get_volume()
    volume = integer(info.get('volume'), 'volume')
    minimum = integer(info.get('minVolume'), 'minVolume')
    maximum = integer(info.get('maxVolume'), 'maxVolume')
    if not minimum <= volume <= maximum:
        raise ValueError('TV reported inconsistent volume/range values.')
    log(label + ': ' + json.dumps(info, ensure_ascii=False))
    if observed:
        observed(dict(info, volume=volume, minVolume=minimum, maxVolume=maximum))
    return volume, minimum, maximum


def run_experiment(api, target, rate_ms, verify_ms, correction, cancel, log,
                   *, clock=time.monotonic, wait=None, observed=None):
    """Serialized equivalent of the remote limiter, followed by one settling check."""
    wait = cancel.wait if wait is None else wait
    def check_cancelled():
        if cancel.is_set():
            raise Cancelled('Stopped; no further requests will be sent.')
    def pause(milliseconds):
        if wait(milliseconds / 1000):
            raise Cancelled('Stopped while waiting; no further requests will be sent.')
        check_cancelled()
    def observe(label):
        check_cancelled()
        return read_volume(api, log, label, observed)[0]

    check_cancelled()
    log('SETTINGS: ' + json.dumps(dict(target=target, rate_ms=rate_ms,
                                     verify_ms=verify_ms, correction=correction)))
    power = api.get_power()
    log('Initial TV power: ' + str(power))
    if power != 'active':
        raise ValueError('Turn on the TV before starting this experiment.')
    check_cancelled()
    current, minimum, maximum = read_volume(api, log, 'INITIAL', observed)
    if type(target) is not int or not minimum <= target <= maximum:
        raise ValueError('Target is outside the TV volume range.')
    direction = 1 if current < target else -1 if current > target else 0
    best, progressed_at = current, clock()
    sends = 0
    while current != target:
        check_cancelled()
        if direction * (current - target) > 0:
            log(f'STOP: crossed target at {current}; no reverse command. {sends} sends.')
            return 'crossed'
        now = clock()
        if direction * (current - best) > 0:
            best, progressed_at = current, now
        if now - progressed_at >= NO_PROGRESS_SECONDS:
            log(f'STOP: no closer volume for five seconds; actual {current}, target {target}. {sends} sends.')
            return 'stalled'
        check_cancelled()
        log(f'SEND {sends+1}: relative {direction:+d}; observed {current}, target {target}')
        api.adjust_volume(direction)
        sends += 1
        pause(rate_ms)
        current = observe('LIMITER READ')

    reached_at = clock()
    log(f'TARGET FIRST REPORTED: {current}; {sends} relative sends. Waiting {verify_ms} ms before verification.')
    pause(verify_ms)
    current = observe('SETTLED CHECK')
    log(f'Verification completed {(clock()-reached_at)*1000:.1f} ms after target was first reported.')
    if current == target:
        log('RESULT: target retained. No correction needed.')
        return 'retained'
    if not direction or current != target - direction:
        log(f'RESULT: target {target}, actual {current}; not one step short in the original direction. No correction.')
        return 'different'
    if not correction:
        log(f'RESULT: one step short ({current} instead of {target}). Correction disabled; would send {direction:+d} once.')
        return 'short'
    check_cancelled()
    log(f'CORRECTION: sending {direction:+d} once. No further correction or limiter restart.')
    api.adjust_volume(direction)
    pause(verify_ms)
    current = observe('AFTER CORRECTION')
    log(f'RESULT: target {target}, actual {current} after one correction; stopping regardless of result.')
    return 'corrected' if current == target else 'correction_unconfirmed'


class Signals(QObject):
    log = Signal(str)
    observed = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, api, settings=None):
        super().__init__()
        self.api = api
        self.settings = settings
        self.cancel = threading.Event()
        self.signals = Signals()

    def run(self):
        try:
            if self.settings is None:
                power = self.api.get_power()
                self.signals.log.emit('REFRESH power: ' + str(power))
                if not self.cancel.is_set():
                    read_volume(self.api, self.signals.log.emit, 'REFRESH', self.signals.observed.emit)
            else:
                run_experiment(self.api, **self.settings, cancel=self.cancel,
                    log=self.signals.log.emit, observed=self.signals.observed.emit)
        except Cancelled as error:
            self.signals.log.emit(str(error))
        except Exception as error:
            self.signals.log.emit(f'STOP: {type(error).__name__}: {error}. No automatic retry.')
        finally:
            self.signals.finished.emit()


class Window(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.setWindowTitle('TV Volume Settling Prototype ' + __version__)
        self.resize(820, 650)
        self.started = time.monotonic()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.worker = None
        self.closing = False
        self.have_reading = False
        self.history = []
        self.api = TvApi(settings, self.log)
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        warning = QLabel('Close Sony Remote and other software volume controllers before testing.\n'
            'Works with TV speakers or TV → CEC → amplifier. No amplifier IPC is used.\n'
            'Choose a safe target. This changes real volume; mute and volume are not restored.\n'
            'Initial power is checked once; there is no extra background polling during the test.')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.current = QLabel('TV volume: not read yet')
        layout.addWidget(self.current)
        form = QFormLayout()
        layout.addLayout(form)
        self.target = QSpinBox()
        self.target.setRange(0, 100)
        self.target.setValue(10)
        form.addRow('Target TV volume', self.target)
        self.rate = QSpinBox()
        self.rate.setRange(1, 10000)
        rate_limit = settings.get('VolumeRateLimit')
        rate = rate_limit.get('RateMs', 500) if isinstance(rate_limit, dict) else 500
        self.rate.setValue(rate if type(rate) is int and 1 <= rate <= 10000 else 500)
        self.rate.setSuffix(' ms')
        form.addRow('Volume command interval', self.rate)
        self.delay = QSpinBox()
        self.delay.setRange(0, 10000)
        self.delay.setValue(500)
        self.delay.setSuffix(' ms')
        form.addRow('Verification delay after target', self.delay)
        self.correction = QCheckBox('Allow one correction if exactly one step short (off by default)')
        layout.addWidget(self.correction)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        self.refresh = QPushButton('Read current volume')
        self.refresh.clicked.connect(lambda: self.launch(None))
        buttons.addWidget(self.refresh)
        self.start = QPushButton('Run test')
        self.start.clicked.connect(self.start_test)
        buttons.addWidget(self.start)
        self.stop = QPushButton('Stop')
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.stop_test)
        buttons.addWidget(self.stop)
        save = QPushButton('Save log')
        save.clicked.connect(self.save_log)
        buttons.addWidget(save)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        layout.addWidget(self.activity)
        self.log('Prototype ready. Read current volume, choose a target, then run the test. No automatic commands at startup.')

    def log(self, message):
        # API calls happen in the worker; marshal their logs to the GUI thread.
        if self.worker is not None and threading.current_thread() is not threading.main_thread():
            self.worker.signals.log.emit(message)
            return
        timestamp = datetime.now().astimezone().isoformat(timespec='milliseconds')
        line = f'{timestamp}  +{time.monotonic()-self.started:9.3f}s  {message}'
        self.history.append(line)
        self.activity.appendPlainText(line)

    def start_test(self):
        self.launch(dict(target=self.target.value(), rate_ms=self.rate.value(),
                         verify_ms=self.delay.value(), correction=self.correction.isChecked()))

    def launch(self, settings):
        if self.worker is not None:
            return
        self.worker = Worker(self.api, settings)
        self.worker.signals.log.connect(self.log)
        self.worker.signals.observed.connect(self.observed)
        self.worker.signals.finished.connect(self.finished)
        for widget in (self.refresh, self.start, self.target, self.rate, self.delay, self.correction):
            widget.setEnabled(False)
        self.stop.setEnabled(True)
        self.pool.start(self.worker)

    def observed(self, info):
        self.current.setText(f'TV volume: {info["volume"]}   Mute: {info.get("mute")}   Range: {info["minVolume"]}–{info["maxVolume"]}')
        self.target.setRange(info['minVolume'], info['maxVolume'])
        if not self.have_reading:
            if self.worker is not None and self.worker.settings is None:
                self.target.setValue(info['volume'])
            self.have_reading = True

    def stop_test(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self.log('STOP requested. An HTTP request already sent cannot be recalled.')
            self.stop.setEnabled(False)

    def finished(self):
        self.worker = None
        for widget in (self.refresh, self.start, self.target, self.rate, self.delay, self.correction):
            widget.setEnabled(True)
        self.stop.setEnabled(False)
        self.log('Idle. Check actual volume/mute on TV, amplifier and computer as appropriate.')
        if self.closing:
            self.close()

    def save_log(self):
        filename = 'TV-volume-settle-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.txt'
        path, _ = QFileDialog.getSaveFileName(self, 'Save experiment log', filename, 'Text (*.txt)')
        if path:
            try:
                Path(path).write_text('\n'.join(self.history) + '\n', encoding='utf-8')
            except OSError as error:
                QMessageBox.warning(self, 'Save failed', str(error))

    def closeEvent(self, event):
        if self.worker is not None:
            self.closing = True
            self.stop_test()
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    try:
        window = Window(load_settings(Path(__file__).resolve().parent))
    except (OSError, ValueError) as error:
        QMessageBox.critical(None, 'TV volume prototype setup', str(error))
        return 1
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
