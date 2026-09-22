# Renders docs/demo/demo.gif from a replay of a real run (see replay.py).
# Run from the repo root: vhs docs/demo/demo.tape
Output docs/demo/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1000
Set Height 580
Set Padding 24
Set Theme "GitHub Dark"
Set TypingSpeed 0ms

Hide
Type "clear && python3 docs/demo/replay.py" Enter
Show
Sleep 16s
