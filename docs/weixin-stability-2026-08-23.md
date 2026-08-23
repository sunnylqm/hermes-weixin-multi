# Weixin Multi stability rollout — 2026-08-23

## Baseline

The SG gateway showed repeated user-visible assistant text during long tool
chains. The `weixin_multi` platform was inheriting Hermes' global
`display.interim_assistant_messages: true` even though iLink/WeChat does not
support editing an already-sent message. Each tool-call preamble was therefore
delivered as a new ordinary WeChat message.

The same sessions also produced repeated iLink `ret=-2` rate-limit responses.
The adapter could schedule multiple sends for one chat concurrently, and the
retry loop treated rate-limit errors like ordinary errors. Inbound poll updates
were also started as independent tasks before the gateway's session guard,
which left a race around context tokens and account routing.

## Design

1. Configure `weixin_multi` as a quiet, non-editable platform: final replies
   only; no interim assistant messages, tool progress, long-running notices,
   or busy acknowledgements.
2. Serialize inbound processing by logical chat (`dm:<sender>` or
   `group:<room>`), including events arriving from different account pollers.
3. Serialize outbound messages by chat and iLink requests by account. Enforce
   a minimum send interval and use a dedicated exponential backoff budget for
   `ret=-2` rate limits.
4. Classify rate-limit failures so Hermes core does not send a second
   plain-text fallback into the same throttle window.
5. Keep retries bounded and observable. Log scheduled inbound events, queueing,
   and deduplication reasons without logging message contents or credentials.
6. Add the same low-noise default to Hermes core for the custom platform key so
   a newly-created profile cannot silently re-enable interim messages.

## Verification checklist

- [ ] `python -m py_compile` passes for plugin modules.
- [ ] Hermes core display-config test passes with `weixin_multi` mapped to the
      low-capability tier.
- [ ] SG and KR profiles contain the platform-specific quiet display settings.
- [ ] After restart, one long tool chain produces one final WeChat reply.
- [ ] Gateway logs show no interim-message flood; rate-limit retries remain
      bounded and account sends are serialized.
- [ ] `git status` is checked before and after deployment; pre-existing user
      changes are preserved.

## Rollback

Disable the new platform-specific display block and restore the previous plugin
commit. Do not delete sync buffers or session history as part of a routine
rollback; preserve them for diagnosis unless a stale-session recovery is
explicitly required.
