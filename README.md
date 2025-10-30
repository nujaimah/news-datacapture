# news-datacapture

This script uses the *Playwright* and *Newspaper* Python libraries to capture PDF screenshots of the CBC News homepage and individual articles, as well as extract data of individual articles including title, author, date posted, etc.

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

The script will open Chromium, load the CBC News homepage, and save a PDF screenshot in your local directory. Then it will retrieve all individual article URLs and open separate Chromium tabs to capture individual PDF screenshots of each article, which will be stored in your local directory. Lastly, the metadata of each article will be extracted and pasted into a Google Sheet. 




