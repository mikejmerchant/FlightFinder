# Contributing to AI Flight Finder Suite

Thanks for your interest in contributing! Here's how to get started.

## Reporting Issues

If something isn't working:

1. Try `--debug` mode first — it saves raw HTML from Google Flights to `/tmp/` so you can see exactly what was scraped
2. Check whether the route actually exists on [Google Flights](https://www.google.com/travel/flights) in your browser
3. Open a GitHub issue with:
   - The exact command you ran (remove your API key if you used `--api-key`)
   - The error message or unexpected output
   - Your OS and Python version (`python --version`)

## Suggesting Features

Open a GitHub issue with the label `enhancement`. Good candidates include:

- Additional currency support
- Caching to avoid re-scraping identical routes
- Multi-city itineraries
- More output formats (CSV, JSON)

## Making a Pull Request

1. Fork the repo and create a branch: `git checkout -b my-feature`
2. Make your changes
3. Test with at least two or three different queries to make sure nothing is broken
4. Keep commits focused — one logical change per commit
5. Open a PR with a clear description of what changed and why

## Code Style

- Standard Python (PEP 8 broadly, but readability over strict compliance)
- Meaningful variable names — this code is meant to be readable
- If you add a new CLI flag, document it in `README.md`
- Don't commit API keys, PDF outputs, or debug HTML files (the `.gitignore` covers these)

## A Note on the Scraper

The Google Flights scraper uses proven CSS selectors based on `aria-label` attributes and `data-gs` spans, which are more stable than class names (which are build-hash-generated and change with every Google deploy). If Google changes their structure and the scraper breaks, the fix usually involves updating the selectors in the `scrape_google_flights()` function. The `--debug` flag is your friend here.
