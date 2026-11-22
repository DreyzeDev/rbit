# Verifying the sandbox on a real Mac

The macOS backend (`sandbox-exec` with a generated Seatbelt profile, `rbit/sandbox/macos.py`)
has **never been run on a Mac**. Its tests only check the text of the generated profile.
Until someone runs the steps below on real hardware, treat macOS isolation as **unverified**.

## 1. Run the built-in check (about a minute)

```bash
git clone <your rbit repo> && cd rbit
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
rbit sandbox status
rbit sandbox check | tee mac-sandbox-check.txt
```

Expected: `backend: sandbox-exec` and every line `PASS` (12 checks on macOS: the Linux-only and
Windows-only checks are skipped). Any `FAIL` line is a real isolation gap on that Mac.

## 2. Confirm `sandbox-exec` itself works on this macOS version

`sandbox-exec` is deprecated by Apple. These two commands show whether the mechanism is
still enforced on your version:

```bash
# must fail (network blocked) and exit non-zero
sandbox-exec -p '(version 1)(allow default)(deny network*)' /usr/bin/curl -sS -m 3 https://example.com
# must fail with "Operation not permitted" (write outside allowed paths blocked)
sandbox-exec -p '(version 1)(allow default)(deny file-write*)' /usr/bin/touch "$HOME/rbit-should-not-exist"
```

If the file `~/rbit-should-not-exist` appears, `sandbox-exec` is not enforcing and the macOS
sandbox must not be relied upon: delete the file and report it.

## 3. Things the built-in check does not cover on macOS (please try)

Run them through a strict sandbox (`.rbit/config.json`: `{"shell_sandbox": "strict"}`) with
`rbit run "<prompt>"`, or call the `rbit.sandbox` API directly:

* read `~/.ssh`, `~/.aws`, `~/.rbit/config.json`, `~/Library/Keychains`, `~/Library/Cookies`,
  `~/Library/Application Support` (the profile only denies `~/.ssh ~/.aws ~/.gnupg ~/.rbit
  ~/.config/gh ~/.netrc` explicitly: **everything else in the home directory is readable**,
  this is a known weakness of the allow-default profile);
* connect to a unix socket outside the workspace (ssh-agent, `docker.sock`);
* `open`, `osascript`, `launchctl submit`, `security find-generic-password` (Keychain);
* write via a symlink inside the workspace that points outside it.

Please send the output of step 1 and the result of each item in step 3.

## What a pass does and does not mean

A pass means: on that Mac, with that macOS version, the listed attacks were blocked. It is
not a proof of safety. See `docs/security/sandbox-audit.md` for the threat model and the known
limits of all three platforms.
