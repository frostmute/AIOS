# paperclip (vendored reference)

Upstream: https://github.com/paperclipai/paperclip (MIT). Only the README, licence,
docs (Markdown) and agent skills are kept here. The runnable server comes from npm
(`paperclipai`) and is installed by `aios paperclip install` into `.aios/runtime/paperclip`
on a private Node 24 runtime, so this folder is reference + skills only.

AIOS mounts `skills/paperclip*` into its agents and watches upstream through the
supervised updater (`aios updates`).
