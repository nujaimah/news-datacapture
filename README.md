# news-datacapture

This script uses *Playwright* with *asyncio* to capture a screenshot of the CBC News homepage up to the “My Local” section.

---

## Requirements

**Python Version:** 3.9 or newer

Install required packages and browser dependencies:

```Shell
# install playwright
pip install playwright
# install chromium browser
playwright install chromium
```
---

## Running the Script

Save the script as `cbc_capture.py` and run:

```Shell
python cbc_capture.py
```

The script will open Chromium, browse the CBC News homepage, and save a png screenshot in your local directory up until the “My Local” section as: "cbc_homepage_before_mylocal.png"




