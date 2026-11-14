#!/usr/bin/env python3
"""Print the total remaining CANN credit across all campaign accounts. Used by on_reset.sh to
decide whether the daily quota has actually rolled over yet -- cron fires on wall clock,
the reset is server-side, and the two can disagree by minutes."""
import os, json, ssl, urllib.request
from concurrent.futures import ThreadPoolExecutor
try:
    import certifi; CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()
def one(a):
    r = urllib.request.Request("https://cannbench.com/api/submissions/credits")
    r.add_header("Authorization", "Bearer " + os.environ.get(a, ""))
    try:
        return json.loads(urllib.request.urlopen(r, timeout=60, context=CTX).read())["credits"].get("remaining", 0)
    except Exception:
        return 0
ACC = ["GITCODE_TOKEN"] + ["GITCODE_TOKEN_%d" % i for i in range(2, 20)]
with ThreadPoolExecutor(max_workers=len(ACC)) as ex:
    print(sum(ex.map(one, ACC)))
