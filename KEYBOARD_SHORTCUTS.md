# Keyboard Shortcut Reference

Keyboard shortcuts in the configuration files use Qt `QKeySequence` **PortableText** syntax.

For normal letters and numbers, simply use the key itself:

```text
A
B
1
2
9
```

This document mainly covers keys whose names are less obvious, such as the numeric keypad, arrow keys, navigation keys, multimedia keys, and modifier combinations.

---

## Mouse bindings

These literal names can be used in `Shortcuts` lists alongside keyboard keys, for example `"VolumeUp": ["Volume Up", "WheelUp"]`:

| Name | Mouse action |
| --- | --- |
| `MouseMiddle` | Press the middle button / wheel |
| `MouseBack` | Press the back side button |
| `MouseForward` | Press the forward side button |
| `WheelUp` | Scroll up |
| `WheelDown` | Scroll down |
| `WheelLeft` | Scroll / tilt left |
| `WheelRight` | Scroll / tilt right |

Mouse bindings work only over the focused remote window. Dropdowns and text fields keep normal mouse behavior. Left/right clicks are reserved; mouse names do not support modifier prefixes and cannot be used in Yamaha's `GlobalShortcuts`. Wheel directions follow the events delivered by your OS and mouse driver.

---

## Modifiers

The following modifiers can be added before another key:

```text
Ctrl+
Shift+
Alt+
Meta+
Num+
```

Examples:

```text
Ctrl+S
Shift+F5
Alt+Enter
Ctrl+Shift+S
Ctrl+Alt+Delete
Meta+Space
```

Multiple modifiers can be combined:

```text
Ctrl+Shift+F5
Ctrl+Alt+Home
Ctrl+Shift+PgDown
```

`Meta` normally corresponds to the **Windows key** on Windows.

`Num` is special: it identifies a key as coming from the **numeric keypad**.

For example:

```text
0
```

means the normal `0` key, while:

```text
Num+0
```

means numeric keypad `0`.

Similarly:

```text
+
```

and:

```text
Num++
```

refer to different keys.

---

# Common keyboard keys

## Arrow keys

```text
Up
Down
Left
Right
```

Examples with modifiers:

```text
Ctrl+Left
Ctrl+Right
Shift+Up
Alt+Down
Ctrl+Shift+Right
```

---

## Navigation keys

```text
Home
End
PgUp
PgDown
Insert
Delete
```

Qt also understands the longer forms:

```text
Page Up
Page Down
Insert
Delete
```

The shorter Qt PortableText forms used by default are:

```text
PgUp
PgDown
Ins
Del
```

Examples:

```text
Ctrl+Home
Ctrl+End
Shift+Home
Shift+End
Ctrl+PgUp
Ctrl+PgDown
```

---

## Enter, Escape, Tab and editing keys

```text
Enter
Return
Escape
Tab
Backtab
Backspace
Space
```

Qt's short name for Escape is also:

```text
Esc
```

### Enter versus Return

Qt distinguishes:

```text
Return
Enter
```

`Return` normally represents the main keyboard Return/Enter key.

`Enter` is traditionally associated with the numeric keypad Enter key, although exact behavior can depend on the platform and keyboard.

When specifically targeting the numeric keypad, prefer:

```text
Num+Enter
```

---

## Lock keys

```text
CapsLock
NumLock
ScrollLock
```

Qt also recognizes:

```text
Caps Lock
Num Lock
Number Lock
Scroll Lock
```

---

## Other standard keyboard keys

```text
Pause
Print
SysReq
Menu
Help
Clear
```

Alternative accepted names include:

```text
Print Screen
System Request
```

`Pause` refers to the traditional **Pause/Break** keyboard key. It is not the media Pause button.

---

# Function keys

Qt supports:

```text
F1
F2
F3
F4
F5
F6
F7
F8
F9
F10
F11
F12
F13
F14
F15
F16
F17
F18
F19
F20
F21
F22
F23
F24
F25
F26
F27
F28
F29
F30
F31
F32
F33
F34
F35
```

Most PC keyboards physically provide only `F1` through `F12`, but Qt can represent up to `F35`.

Examples:

```text
Ctrl+F1
Shift+F5
Alt+F12
Ctrl+Shift+F10
```

---

# Numeric keypad

Use the `Num+` modifier to explicitly select keys on the numeric keypad.

## Numeric keypad digits

```text
Num+0
Num+1
Num+2
Num+3
Num+4
Num+5
Num+6
Num+7
Num+8
Num+9
```

---

## Numeric keypad operators

```text
Num++
Num+-
Num+*
Num+/
```

The double `+` in:

```text
Num++
```

is intentional.

It means:

```text
Num + "+"
```

or, in other words, the `+` key on the numeric keypad.

---

## Numeric keypad decimal key

Usually:

```text
Num+.
```

The physical key and generated character can depend on keyboard layout and regional decimal separator.

---

## Numeric keypad Enter

```text
Num+Enter
```

---

## Numeric keypad navigation

With Num Lock disabled, numeric keypad keys can also generate navigation functions. These can be described explicitly:

```text
Num+Home
Num+End
Num+PgUp
Num+PgDown
Num+Insert
Num+Delete
Num+Up
Num+Down
Num+Left
Num+Right
```

This can be useful when the normal navigation key and its numeric-keypad equivalent need different shortcuts.

---

## Modifiers combined with the numeric keypad

`Num` can be combined with the other modifiers:

```text
Ctrl+Num+0
Shift+Num+1
Alt+Num+Enter
Ctrl+Shift+Num+5
```

---

# Punctuation and symbol keys

Printable symbols can normally be used directly.

Common examples:

```text
`
-
=
[
]
\
;
'
,
.
/
```

Other printable symbols can also be used, for example:

```text
+
*
?
:
_
(
)
```

Examples with modifiers:

```text
Ctrl+,
Ctrl+.
Alt+/
Ctrl+;
Ctrl+[
Ctrl+]
```

## The plus key

Because `+` is also the separator between modifiers and keys, a shortcut containing the actual plus key can look unusual.

For example:

```text
Ctrl++
```

means **Ctrl + Plus**.

Likewise:

```text
Num++
```

means **numeric keypad Plus**.

And:

```text
Ctrl+Num++
```

means **Ctrl + numeric keypad Plus**.

---

# Multimedia keys

Qt recognizes common multimedia keyboard buttons.

## Volume

```text
Volume Up
Volume Down
Volume Mute
```

---

## Playback

```text
Media Play
Media Pause
Media Stop
Media Previous
Media Next
Media Record
Toggle Media Play/Pause
Media Rewind
Media Fast Forward
```

Additional audio controls include:

```text
Audio Repeat
Audio Random Play
Audio Cycle Track
```

---

## Audio adjustment keys

Some multimedia keyboards expose:

```text
Bass Boost
Bass Up
Bass Down
Treble Up
Treble Down
```

---

## Microphone controls

```text
Microphone Mute
Microphone Volume Up
Microphone Volume Down
```

---

# Browser and web keys

Qt recognizes many dedicated browser buttons:

```text
Back
Forward
Stop
Refresh
Home Page
Favorites
Search
Open URL
WWW
History
Add Favorite
Hot Links
```

These refer to dedicated keyboard buttons, not necessarily normal shortcuts such as `Alt+Left` or `F5`.

For example:

```text
Back
```

means a dedicated Back key.

It is different from:

```text
Alt+Left
```

even though many applications perform the same action for both.

---

# Application-launch keys

Dedicated multimedia keyboards may expose buttons for launching applications.

Common names include:

```text
Launch Mail
Launch Media
Calculator
Calendar
Documents
Browser
Game
Terminal
Pictures
Music
Video
Word Processor
Spreadsheet
```

Qt additionally defines generic launch buttons:

```text
Launch (0)
Launch (1)
Launch (2)
Launch (3)
Launch (4)
Launch (5)
Launch (6)
Launch (7)
Launch (8)
Launch (9)
Launch (A)
Launch (B)
Launch (C)
Launch (D)
Launch (E)
Launch (F)
Launch (G)
Launch (H)
```

Whether these exist depends entirely on the keyboard and its driver.

---

# Display, keyboard and power keys

## Display brightness

```text
Monitor Brightness Up
Monitor Brightness Down
Adjust Brightness
Adjust contrast
Display
```

---

## Keyboard illumination

```text
Keyboard Light On/Off
Keyboard Brightness Up
Keyboard Brightness Down
Keyboard
```

---

## Power and sleep

```text
Power Off
Power Down
Standby
Sleep
Suspend
Hibernate
Wake Up
Screensaver
```

Be careful when assigning actual power-management buttons. Windows or firmware may handle some of them independently of the application.

---

## Other hardware controls

```text
Eject
Battery
Bluetooth
Wireless
Ultra Wide Band
Touchpad Toggle
Touchpad On
Touchpad Off
```

---

# TV and remote-control style keys

Qt defines several keys commonly found on media-center keyboards and remote controls:

```text
Red
Green
Yellow
Blue

Channel Up
Channel Down

Guide
Info
Settings

Subtitle
Top Menu
View
Time
```

Support depends on whether Windows and the device driver expose the physical button as the corresponding Qt key.

---

# Editing and application command keys

Some keyboards have dedicated command buttons rather than requiring normal keyboard shortcuts.

Qt recognizes:

```text
New
Open
Close
Save
Copy
Cut
Paste
Find
Undo
Redo
Reply
Send
Mail Forward
Spellchecker
Printer
Execute
Exit
```

These are dedicated keys.

For example:

```text
Copy
```

is not the same physical shortcut as:

```text
Ctrl+C
```

even though both may perform a Copy operation in an application.

---

# Additional special keys

Qt also defines names for a number of less common keyboard and device buttons:

```text
LightBulb
Shop
Finance
Community
Back Forward
Application Left
Application Right
Book
CD
Go
iTouch
Logoff
Market
Meeting
Memo
Keyboard Menu
Menu PB
My Sites
News
Home Office
Option
Phone
Reload
Rotate Windows
Rotation PB
Rotation KB
Split Screen
Support
Task Panel
To-do list
Tools
Travel
XFer
Zoom In
Zoom Out
Away
Messenger
WebCam
Select
Yes
No
Cancel
Play
Zoom
```

These names are supported by Qt, but many normal PC keyboards will never generate them.

---

# Phone and camera device keys

Qt also defines several keys mainly intended for phones, handheld devices, specialist keyboards, or unusual controllers:

```text
Call
Hangup
Toggle Call/Hangup
Flip
Voice Dial
Last Number Redial
Camera Shutter
Camera Focus
```

---

# Context keys

Some devices expose generic context buttons:

```text
Context1
Context2
Context3
Context4
```

---

# Japanese keyboard keys

Qt recognizes the following Japanese keyboard keys:

```text
Kanji
Muhenkan
Henkan
Romaji
Hiragana
Katakana
Hiragana Katakana
Zenkaku
Hankaku
Zenkaku Hankaku
Touroku
Massyo
Kana Lock
Kana Shift
Eisu Shift
Eisu toggle
Code input
Multiple Candidate
Previous Candidate
```

---

# Korean keyboard keys

Qt recognizes the following Korean keyboard keys:

```text
Hangul
Hangul Start
Hangul End
Hangul Hanja
Hangul Jamo
Hangul Romaja
Hangul Jeonja
Hangul Banja
Hangul PreHanja
Hangul PostHanja
Hangul Special
```

---

# Using modifiers

A shortcut consists of a key, optionally preceded by one or more modifiers.

Basic examples:

```text
Ctrl+A
Shift+F1
Alt+Enter
Meta+Space
```

Several modifiers can be combined:

```text
Ctrl+Shift+A
Ctrl+Alt+Delete
Ctrl+Shift+Home
Alt+Shift+Left
```

The keypad modifier can be combined in exactly the same way:

```text
Num+0
Ctrl+Num+0
Shift+Num+Enter
Ctrl+Shift+Num++
```

The modifier order is not especially important for understanding the shortcut, but keeping a consistent order makes the JSON easier to read.

A good convention is:

```text
Ctrl+Alt+Shift+Num+KEY
```

Only include the modifiers that are actually required.

---

An empty string can be used for configuration options where the application supports disabling a shortcut:

```text
"SomeShortcut": ""
```

---

# Keyboard layout considerations

Letter shortcuts are generally straightforward:

```text
Ctrl+A
Ctrl+F
Ctrl+Shift+S
```

Symbol keys are more complicated because their physical locations and required modifiers vary between keyboard layouts.

For example:

```text
Ctrl++
```

may require different physical key combinations on different national keyboard layouts.

For portable configuration files, named keys such as:

```text
Home
End
Delete
PgDown
Media Play
Volume Up
```

are generally less ambiguous than punctuation-heavy shortcuts.

---

# Important notes

- Key names are interpreted using Qt `QKeySequence` PortableText syntax.
- Normal letters and digits can be written directly.
- `Num+` identifies numeric-keypad keys.
- `Ctrl`, `Shift`, `Alt`, `Meta`, and `Num` can be combined.
- Some special keys depend on keyboard hardware and Windows driver support.
- A key being defined by Qt does not guarantee that a particular keyboard can generate it.
- Windows may reserve or independently process certain system, power, and multimedia keys.
- The remotes may use additional native Windows handling for shortcuts that need behavior Qt alone cannot provide.
- When configuring conflicting shortcuts between the Sony and Yamaha remotes, use the exact same key and modifier combination if the shortcuts are intended to overlap.

For Qt's underlying syntax and key definitions, see the Qt documentation for `QKeySequence`, `Qt::Key`, and `Qt::KeyboardModifier`.