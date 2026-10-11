---
paths:
  - "README.md"
  - "CHANGELOG.md"
  - "CONTRIBUTING.md"
  - "SECURITY.md"
  - "SUPPORT.md"
  - "GOVERNANCE.md"
  - "docs/**/*.md"
---

# Documentation rules

- **Plain words, short sentences.** Write for a reader whose first language is not English. Say "use", not "utilize". One idea per sentence.
- **Show, then tell.** A command, a config snippet or an output sample beats a paragraph.
- **Document what exists.** Never describe a rule or option as working before it does. Mark planned work as "Planned".
- **Keep the README and the CLI in step.** A new or changed option updates the options table, the `[tool.slopfence]` example, and the line that lists which options override config. A new rule updates the detector table.
- **Network behaviour is a promise.** Anything that changes what is sent, or to which host, updates "What slopfence sends over the network" and `SECURITY.md`.
- **CHANGELOG** follows Keep a Changelog: entries go under `[Unreleased]` in `Added`, `Changed` or `Fixed`, each with a link to its issue. Say what changed for the user, not how the code changed.
- Never print an index URL in an example. Use a placeholder host.
- Check your work with `npx markdownlint-cli2 "**/*.md" "!.venv/**"`.
