"""meerpic-agent: the half that does the work.

It walks the library, reads each file's metadata, draws thumbnails, makes
browser-playable copies of videos, computes the search vectors and runs
rclone when the Sync button asks for it. The web app only reads what this
writes; the two share Postgres and the cache directory and nothing else.
"""
