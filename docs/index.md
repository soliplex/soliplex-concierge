# Soliplex Concierge

`soliplex-concierge` is a [Soliplex](https://github.com/soliplex/soliplex)
extension for LLM-driven, on-demand room requests. Users chat with a self-serve
"about" room to request **access to a non-public room** or the **creation of a
new room**; the concierge files each request as a tracking issue on a Gitea
repository, and an administrator resolves it.

The documentation is organized around the two task areas:

- **[Rooms](tasks/rooms/index.md)** — the self-serve side. How the about-room
  concierge collects a request and files it as an issue, and how to
  [set it up](tasks/rooms/setup.md) in an installation.
- **[Admin](tasks/admin/index.md)** — the resolution side. How an administrator
  triages the filed issues and carries out the requested
  [room creation](tasks/admin/room_creation.md) or
  [room access](tasks/admin/room_access.md).
