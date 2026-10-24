"""Direct Sony/Yamaha CEC handover experiment. Requires only PySide6.

Place SonyRemote.json and YamahaRemote.json beside this script. No IPC, imports
from remotes, setting writes, automatic mute changes or automatic restoration.
"""
import ipaddress
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget)

__version__ = '2026.10.24.2'
TV_INPUT = 'tv'
OTHER_INPUT = 'usb_dac'
POLL_SECONDS = .25
SEND_SECONDS = .20
BURST_SECONDS = 2.0
OBSERVE_SECONDS = 3.0
TIMEOUT_SECONDS = 2.0
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_settings(folder):
    def read(filename):
        value = json.loads((folder / filename).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError(filename + ' must contain a JSON object.')
        return value
    tv, amp = read('SonyRemote.json'), read('YamahaRemote.json')
    for name, config in (('SonyRemote.json', tv), ('YamahaRemote.json', amp)):
        if not isinstance(config.get('Host'), str) or not config['Host'].strip():
            raise ValueError(name + ' needs a non-empty Host.')
    if not isinstance(tv.get('PreSharedKey'), str) or not tv['PreSharedKey'].strip():
        raise ValueError('SonyRemote.json needs a non-empty PreSharedKey.')
    return tv, amp


def base_url(host):
    host = host.strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if any(char in host for char in '/:@?#') or any(char.isspace() for char in host):
            raise ValueError('Host must be a hostname or IP address, without a URL or port.')
    else:
        if address.version == 6:
            host = '[' + host.replace('%', '%25') + ']'
    return 'http://' + host


class Device:
    def __init__(self, settings, role):
        self.role = role
        self.url = base_url(settings['Host'])
        self.psk = settings.get('PreSharedKey', '')
        self.request_id = 0

    def read_json(self, request):
        with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode('utf-8'))
        if not isinstance(result, dict):
            raise ValueError('Device did not return a JSON object.')
        return result

    def tv_rpc(self, service, method, params=None, version='1.0'):
        self.request_id += 1
        payload = dict(method=method, id=self.request_id,
                       params=[] if params is None else params, version=version)
        request = urllib.request.Request(self.url + '/sony/' + service,
            data=json.dumps(payload).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'X-Auth-PSK': self.psk})
        result = self.read_json(request)
        if 'error' in result:
            raise RuntimeError('TV rejected request: ' + repr(result['error']))
        if not isinstance(result.get('result'), list):
            raise ValueError('TV response has no result list.')
        return result

    def amp_get(self, path):
        result = self.read_json(self.url + '/YamahaExtendedControl/v1/' + path)
        if type(result.get('response_code')) is not int or result['response_code'] != 0:
            raise RuntimeError('Amplifier rejected request (response_code=' + repr(result.get('response_code')) + ').')
        return result

    def poll(self):
        if self.role == 'amplifier':
            data = self.amp_get('main/getStatus')
            return {key: data.get(key) for key in ('power', 'input', 'volume', 'max_volume', 'mute')}
        power = self.tv_rpc('system', 'getPowerStatus')['result'][0]['status']
        if power != 'active':
            return dict(power=power, volume=None, mute=None)
        outputs = self.tv_rpc('audio', 'getVolumeInformation')['result'][0]
        info = next((item for item in outputs if item.get('target') == 'speaker'), None)
        if info is None:
            raise ValueError('TV did not report its speaker volume target.')
        return dict(power=power, **{key: info.get(key) for key in ('target', 'volume', 'mute', 'minVolume', 'maxVolume')})

    def set_input(self, input_id):
        return self.amp_get('main/setInput?input=' + quote(input_id, safe=''))

    def set_tv_volume(self, volume):
        # Exact absolute-volume method/version from the user's verified prototype.
        return self.tv_rpc('audio', 'setAudioVolume',
            [{'target': 'speaker', 'volume': str(volume), 'ui': 'on'}], '1.2')


class Signals(QObject):
    started = Signal(str, str, float)
    done = Signal(str, str, object, object, float)


class Job(QRunnable):
    def __init__(self, role, kind, function):
        super().__init__()
        self.role, self.kind, self.function = role, kind, function
        self.signals = Signals()

    def run(self):
        started = time.monotonic()
        self.signals.started.emit(self.role, self.kind, started)
        result = error = None
        try:
            result = self.function()
        except Exception as caught:
            error = caught
        self.signals.done.emit(self.role, self.kind, result, error, started)


class Window(QMainWindow):
    def __init__(self, tv, amp):
        super().__init__()
        self.devices = dict(tv=Device(tv, 'tv'), amplifier=Device(amp, 'amplifier'))
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.jobs = {}
        self.state = dict(tv=None, amplifier=None)
        self.observed = dict(tv=0., amplifier=0.)
        self.sample_started = dict(tv=0., amplifier=0.)
        self.next_poll = dict(tv=0., amplifier=0.)
        self.stage = None
        self.manual_input = self.manual_tv_target = None
        self.closed = False
        self.started = time.monotonic()
        self.baseline = None
        self.sent = 0
        self.changes = []
        self.setWindowTitle('TV / amplifier CEC handover prototype ' + __version__)
        self.resize(850, 570)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.setCentralWidget(panel)
        warning = QLabel('Close BOTH normal remotes to avoid interference. Use a silent TV source and a safe amplifier volume.\n'
            'This test changes amplifier input and TV volume. CEC may also change amplifier volume or mute.\n'
            'No automatic restoration: check TV/amplifier volume, mute and input afterward.')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.labels = {}
        for role in self.devices:
            self.labels[role] = QLabel(role + ': waiting for status')
            self.labels[role].setWordWrap(True)
            layout.addWidget(self.labels[role])
        self.auto_watch = QCheckBox('Automatically run the TV volume burst when amplifier changes tv → usb_dac')
        self.auto_watch.setChecked(True)
        layout.addWidget(self.auto_watch)
        row = QHBoxLayout()
        self.input_buttons = []
        for input_id in (TV_INPUT, OTHER_INPUT):
            button = QPushButton('Amplifier input: ' + input_id)
            button.clicked.connect(lambda checked=False, value=input_id: self.switch(value))
            self.input_buttons.append(button)
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel('Absolute TV target:'))
        self.target = QSpinBox()
        self.target.setRange(0, 100)
        self.target.setValue(10)
        row.addWidget(self.target)
        self.single = QPushButton('Send TV volume once')
        self.single.clicked.connect(self.send_single)
        row.addWidget(self.single)
        self.test = QPushButton('Switch to usb_dac + test 2-second burst')
        self.test.clicked.connect(self.start_test)
        row.addWidget(self.test)
        self.stop = QPushButton('Stop test')
        self.stop.clicked.connect(self.stop_test)
        row.addWidget(self.stop)
        layout.addLayout(row)
        self.status = QLabel('Waiting for fresh device status.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        save = QPushButton('Save log…')
        save.clicked.connect(self.save_log)
        layout.addWidget(save)
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.message('Polling each device about every 250 ms; one outstanding request per device. '
                     'TV reads include separate power and volume requests. Inputs: tv -> usb_dac.')
        self.update_controls()

    def message(self, text):
        self.log.appendPlainText(f'{time.monotonic() - self.started:8.3f}s  {text}')

    def fresh(self, role):
        return self.state[role] is not None and time.monotonic() - self.observed[role] < 1.5

    def submit(self, role, kind, function):
        if self.closed or role in self.jobs:
            return False
        job = Job(role, kind, function)
        self.jobs[role] = job
        job.signals.started.connect(self.request_started)
        job.signals.done.connect(self.completed)
        self.pool.start(job)
        self.update_controls()
        return True

    def request_started(self, role, kind, started):
        if self.closed or kind == 'poll':
            return
        relative = (f'; +{started - self.burst_start:.3f}s after usb_dac detection'
                    if kind.startswith('burst volume') else '')
        self.message(f'{role} {kind} REQUEST START +{started - self.started:.3f}s{relative}')

    def completed(self, role, kind, result, error, request_started):
        self.jobs.pop(role, None)
        if self.closed:
            return
        if error is not None:
            # Do not log request headers, credentials, hosts or exception messages.
            self.message(f'{role} {kind} FAILED: {type(error).__name__}')
            self.state[role] = None
            self.labels[role].setText(role + ': request failed (' + type(error).__name__ + ')')
            self.next_poll[role] = time.monotonic() + .5
            if self.stage:
                self.finish('Test stopped on a device request failure. Check device settings manually.')
        elif kind == 'poll':
            previous = self.state[role] if self.fresh(role) else None
            previous_request = self.sample_started[role]
            changed = result != self.state[role]
            self.state[role] = result
            self.observed[role] = time.monotonic()
            self.sample_started[role] = request_started
            self.labels[role].setText(role + ': ' + json.dumps(result))
            if changed or self.stage:
                self.message(f'{role} status (request +{request_started - self.started:.3f}s): ' + json.dumps(result))
            if (role == 'amplifier' and self.stage is None and self.auto_watch.isChecked()
                    and previous and previous.get('power') == 'on' and previous.get('input') == TV_INPUT
                    and result.get('power') == 'on' and result.get('input') == OTHER_INPUT):
                self.message(f'AUTO detected tv → usb_dac. Last tv-input poll started +{previous_request - self.started:.3f}s; '
                             f'usb_dac report received +{self.observed[role] - self.started:.3f}s. '
                             'Exact physical switch time is not observable.')
                if self.tv_target_valid():
                    self.begin_burst(result, 'Automatic handover', previous)
                else:
                    self.message('Automatic burst skipped: no fresh, active TV status with a valid target range.')
            if self.stage and role == 'amplifier':
                if result.get('power') != 'on':
                    self.finish('Test stopped: amplifier is not on.')
                elif self.stage in ('burst', 'observe') and result.get('input') != OTHER_INPUT:
                    self.finish('Test stopped: amplifier left usb_dac.')
                elif self.stage == 'wait_input' and request_started >= self.switch_completed and result.get('input') == OTHER_INPUT:
                    self.begin_burst(result, 'Button-driven handover')
                elif self.stage in ('burst', 'observe') and result.get('volume') != self.baseline.get('volume'):
                    if not self.changes or self.changes[-1] != result.get('volume'):
                        self.changes.append(result.get('volume'))
                        self.message('AMPLIFIER VOLUME CHANGED after handover: ' + repr(result.get('volume')))
            if self.stage and role == 'tv' and result.get('power') != 'active':
                self.finish('Test stopped: TV is not on.')
        else:
            self.message(f'{role} {kind} acknowledged (request +{request_started - self.started:.3f}s; '
                         f'response after {time.monotonic() - request_started:.3f}s).')
            self.next_poll[role] = 0
            if kind == 'test switch' and self.stage == 'switching':
                self.stage = 'wait_input'
                self.switch_completed = time.monotonic()
        self.update_controls()

    def tv_target_valid(self):
        state = self.state['tv'] or {}
        minimum, maximum = state.get('minVolume'), state.get('maxVolume')
        return (self.fresh('tv') and state.get('power') == 'active'
                and type(minimum) is int and type(maximum) is int
                and minimum <= self.target.value() <= maximum)

    def update_controls(self):
        idle = self.stage is None
        for button in self.input_buttons:
            button.setEnabled(idle and self.fresh('amplifier')
                              and self.state['amplifier'].get('power') == 'on')
        self.single.setEnabled(idle and self.tv_target_valid())
        self.test.setEnabled(idle and self.tv_target_valid() and self.fresh('amplifier')
                             and self.state['amplifier'].get('power') == 'on'
                             and self.state['amplifier'].get('input') == TV_INPUT)
        if self.status.text() == 'Waiting for fresh device status.' and self.fresh('tv') and self.fresh('amplifier'):
            self.status.setText('Ready. Watch tv → usb_dac automatically with the checkbox, or use the test button.')
        self.auto_watch.setEnabled(idle)
        self.target.setEnabled(idle)
        self.stop.setEnabled(not idle)

    def switch(self, input_id):
        self.message('Request amplifier input ' + input_id)
        self.manual_input = input_id

    def send_single(self):
        if self.tv_target_valid():
            value = self.target.value()
            self.message('Request ONE absolute TV volume ' + str(value))
            self.manual_tv_target = value

    def start_test(self):
        if not self.test.isEnabled():
            return
        self.sent = 0
        self.changes = []
        self.test_target = self.target.value()
        self.manual_input = self.manual_tv_target = None
        self.stage = 'wait_switch'
        self.test_deadline = time.monotonic() + 8
        self.message('START TEST. Before switch: amplifier=' + json.dumps(self.state['amplifier'])
                     + '; tv=' + json.dumps(self.state['tv']) + '; target=' + str(self.test_target))
        self.status.setText('Switching amplifier to usb_dac; waiting for confirmation before TV commands.')

    def begin_burst(self, status, reason, previous=None):
        self.sent = 0
        self.changes = []
        self.manual_input = self.manual_tv_target = None
        self.test_target = self.target.value()
        self.baseline = status.copy()
        self.burst_start = time.monotonic()
        self.burst_end = self.burst_start + BURST_SECONDS
        self.next_send = self.burst_start
        self.stage = 'burst'
        self.message(reason + ': confirmed usb_dac BEFORE TV sends. Amplifier baseline: ' + json.dumps(self.baseline))
        if previous is not None:
            self.message('Previous tv-input sample: ' + json.dumps(previous))
        self.status.setText('usb_dac detected: starting automatic TV volume burst.')
        self.send_burst_if_due(self.burst_start)

    def send_burst_if_due(self, now):
        if self.stage == 'burst' and self.next_send <= now < self.burst_end and 'tv' not in self.jobs:
            self.sent += 1
            self.message(f'Burst TV SET #{self.sent}: absolute {self.test_target}; queued +{now - self.burst_start:.3f}s after detection')
            value = self.test_target
            self.submit('tv', 'burst volume ' + str(value), lambda: self.devices['tv'].set_tv_volume(value))
            self.next_send = now + SEND_SECONDS  # Skip missed slots; never build a backlog.

    def stop_test(self):
        self.auto_watch.setChecked(False)
        self.finish('Stopped by user; automatic watching disabled. In-flight HTTP requests may still complete.')

    def finish(self, message):
        self.stage = None
        self.message(message)
        self.status.setText(message + ' Check volume/mute/input on both devices.')
        self.update_controls()

    def tick(self):
        now = time.monotonic()
        if self.stage:
            if any(not self.fresh(role) for role in self.devices):
                self.finish('Test stopped: device status became stale.')
            elif not self.tv_target_valid():
                self.finish('Test stopped: TV target is outside its reported range, or TV is not active.')
            elif self.stage in ('wait_switch', 'switching', 'wait_input') and now >= self.test_deadline:
                self.finish('Test stopped: usb_dac was not confirmed within eight seconds.')
            elif self.stage == 'wait_switch' and 'amplifier' not in self.jobs:
                self.stage = 'switching'
                self.submit('amplifier', 'test switch', lambda: self.devices['amplifier'].set_input(OTHER_INPUT))
            elif self.stage == 'burst':
                if now >= self.burst_end:
                    self.stage = 'observe'
                    self.observe_end = now + OBSERVE_SECONDS
                    self.message(f'Burst finished: {self.sent} sends started. Observing for three more seconds; no more volume commands.')
                else:
                    self.send_burst_if_due(now)
                self.status.setText(
                    f'TV burst: {self.sent} sends; amplifier baseline raw {self.baseline.get("volume")}. '
                    if self.stage == 'burst' else 'Observing for delayed CEC reactions; no more volume commands.')
            elif self.stage == 'observe' and now >= self.observe_end:
                if self.changes:
                    self.finish('RESULT: amplifier volume changes observed: ' + repr(self.changes) + '. Review the log; this does not identify their cause.')
                else:
                    self.finish('RESULT: no amplifier volume change observed in this test. This is not a guarantee for other timing or device states.')
        if self.stage is None:
            if self.manual_input is not None and 'amplifier' not in self.jobs:
                value, self.manual_input = self.manual_input, None
                if self.fresh('amplifier') and self.state['amplifier'].get('power') == 'on':
                    self.submit('amplifier', 'input ' + value, lambda value=value: self.devices['amplifier'].set_input(value))
            if self.manual_tv_target is not None and 'tv' not in self.jobs:
                value, self.manual_tv_target = self.manual_tv_target, None
                if self.tv_target_valid():
                    self.submit('tv', 'set volume ' + str(value), lambda value=value: self.devices['tv'].set_tv_volume(value))
        for role, device in self.devices.items():
            if now >= self.next_poll[role] and role not in self.jobs:
                # During the burst, don't let a two-request TV poll delay a due send.
                if role == 'tv' and self.stage == 'burst' and now + .05 >= self.next_send:
                    continue
                self.next_poll[role] = now + POLL_SECONDS
                self.submit(role, 'poll', device.poll)
        self.update_controls()

    def save_log(self):
        name = 'CEC-handover-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.txt'
        path, _ = QFileDialog.getSaveFileName(self, 'Save test log', name, 'Text files (*.txt)')
        if path:
            try:
                Path(path).write_text('Prototype ' + __version__ + '\n' + self.log.toPlainText() + '\n', encoding='utf-8')
            except OSError:
                QMessageBox.warning(self, 'Save failed', 'Could not write the log file.')

    def closeEvent(self, event):
        self.closed = True
        self.stage = None
        self.timer.stop()
        self.pool.clear()
        event.accept()


def main():
    app = QApplication(sys.argv)
    try:
        tv, amp = load_settings(Path(__file__).resolve().parent)
        window = Window(tv, amp)
    except Exception as error:
        QMessageBox.critical(None, 'Prototype setup failed',
            'Place SonyRemote.json and YamahaRemote.json beside the prototype.\n'
            'Check their Host and TV PreSharedKey settings.\n\n' + type(error).__name__)
        return 1
    window.show()
    result = app.exec()
    window.pool.waitForDone()  # Keep workers alive until bounded in-flight requests finish.
    return result


if __name__ == '__main__':
    raise SystemExit(main())
