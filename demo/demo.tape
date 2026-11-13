# rbit demo tape — https://github.com/charmbracelet/vhs
#
# Record from the repo root:
#   vhs demo/demo.tape        (produces demo/demo.gif)
#
# The session below works offline: without a configured provider rbit falls back
# to the built-in smart-local agent, so no API keys are needed (and none are used).

Output demo/demo.gif

Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 720
Set Padding 24
Set Theme "Dracula"

# --- 1) CLI help ---
Type "rbit --help"
Enter
Sleep 3s

Ctrl+L
Sleep 500ms

# --- 2) Launch the TUI ---
Type "rbit"
Enter
Sleep 3s

# --- 3) Fake session prompt (typed into the TUI input bar) ---
Type "добавь функцию is_palindrome в utils.py и тест к ней"
Enter

# --- 4) Thinking indicator + tool / diff cards appear as the agent works ---
Sleep 4s
Sleep 6s

# --- 5) Leave the session ---
Ctrl+Q
Sleep 1s
