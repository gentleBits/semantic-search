# Security

Please report a vulnerability privately, through GitHub's "Report a vulnerability" on this repository's Security tab,
not in a public issue. Include what you found and how to reproduce it.

The app is meant to run on your own machine (it listens on 127.0.0.1 by default). If you serve it to others, add
accounts first (`resumes users add NAME`), put it behind a TLS proxy, and keep `.resumes/` (accounts, keys in
`settings.json`, the cookie secret) readable by the app's user only.

If you ever paste a key somewhere public by mistake, revoke it at its provider first.
