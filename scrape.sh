#!/bin/bash
set -e

# The Courts Portal is plain HTTP, but judgments.fedcourt.gov.au sits behind
# Cloudflare's managed challenge, so judgments are fetched with a real Chrome
# (headful, under Xvfb) via nodriver. xvfb-run gives Chrome a display so it
# runs headful, which is far less likely to be flagged than headless. Chrome
# is only started when there's a judgment that hasn't been saved yet.
#
# The scraper refreshes matters/<slug>/ for each file listed in matters.txt.
# Changes are pushed to ntfy.sh when NTFY_TOPIC is set.
xvfb-run -a python scrape.py "$@"
