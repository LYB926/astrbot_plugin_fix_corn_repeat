# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-08-17

- Add an at-most-once send guard for active-agent Cron executions.
- End the Agent run after the first successful `send_message_to_user` call.
- Allow retries after pre-send validation errors while suppressing retries after
  ambiguous transport failures.
- Add runtime version, signature, cached-tool, hot-reload, and unload safeguards.
