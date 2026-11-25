# Email notifications

Set `UPDOCK_SMTP_HOST` and `UPDOCK_SMTP_TO` to get emails for updates, rollbacks, failed updates and (in check-only mode) newly available images. Everything that happens during one scheduled run arrives as a single email; manual actions email immediately. "Update available" is sent once per new image, not every run. Use **Test email** in the header to check the settings; every send (or failure) is in the logs as a `notify` event.

Emails are HTML (with a plain-text copy for clients that prefer it): one card per event, colour-coded, with the image, old → new version and image ID, when each image was built, the Compose project, how long the update took, whether the old image was removed, the error or rollback reason, and buttons for the release notes and for opening Updock (`UPDOCK_URL`). Runs with several events start with a summary ("2 updated, 1 rolled back"). Update subjects include the new version: `[updock] swafarewatcher: updated to 0.20.0`.

| Var | Default | |
|---|---|---|
| `UPDOCK_SMTP_HOST` | empty | SMTP server; empty disables email |
| `UPDOCK_SMTP_PORT` | `587` | `465` for SSL |
| `UPDOCK_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none` |
| `UPDOCK_SMTP_USER` / `UPDOCK_SMTP_PASSWORD` | empty | Login, if the server needs one |
| `UPDOCK_SMTP_FROM` | user or `updock@<host>` | Sender address |
| `UPDOCK_SMTP_TO` | empty | Recipients, comma-separated |
| `UPDOCK_NOTIFY_EVENTS` | `update,rollback,error,available,approval,disk,unhealthy,healthy,self-update` | Also: `deploy`, `delete` |
| `UPDOCK_INSTANCE` | host name | Name shown in subjects (handy with several hosts) |
| `UPDOCK_URL` | empty | Link to the UI included in emails |
