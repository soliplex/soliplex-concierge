# Administration & status

## Resolving filed requests

Requests filed by the concierge land as issues in the tracking repository. The
`soliplex-concierge-admin` skill drives the resolution workflow: its scripts
read the open request issues, comment on them, and close (resolve) them once the
requested operation has been performed.

The actual provisioning those requests ask for — creating rooms and granting
access — is driven through the
[`soliplex-template`](https://github.com/soliplex/soliplex-template) skill (see
the admin skill's workflow).

## Status

The issue-filing concierge (the `about_soliplex` room, its `create_gitea_issue`
tool, and the `soliplex-concierge-room` skill) is implemented, as is the
`soliplex-concierge-admin` skill, whose scripts read, comment on, and close
(resolve) the filed request issues. The actual provisioning those requests ask
for — creating rooms and granting access — is driven through the
`soliplex-template` skill; turnkey automation of that step is a planned
follow-up.
