# Feeds: Vacancy Bell + Roz Quiz content

`.github/workflows/feeds.yml` runs every 15 minutes on GitHub Actions and
publishes JSON to the `feed-data` branch. The format is in [SCHEMA.md](SCHEMA.md).

## Setup (once)

Add the DeepSeek key as a repository secret: Settings → Secrets and variables
→ Actions → New repository secret, name `DEEPSEEK_API_KEY`. Without it the
run only writes `status.json` saying the key is missing.

## How a job notice gets published

1. Each page in `sources.json` is read; links that look like notices
   (recruitment, admit card, result, answer key, syllabus) are new candidates.
   The first time a source is seen only its newest 6 notices are processed.
2. The notice (HTML or PDF; scanned PDFs go through OCR) is sent to
   `deepseek-flash`, which returns every fact with an exact quote.
3. Code checks every fact: the quote must be in the notice; dates, post
   counts, ages and fees must be written in that quote; English and Hindi
   must carry the same numbers; links must be in the notice. Failed facts go
   back to the model with the reason, up to 3 rounds. Facts that still fail
   are dropped.
4. `deepseek-v4-pro` checks the remaining facts against the notice on its
   own. Anything it can't confirm is dropped.
5. A post missing its title or organisation, a job with no confirmed date,
   or a post that lost more than 40% of its facts is not published. It is
   listed in `review/index.json` on the feed-data branch instead.

## Quiz content

- PIB press releases are collected all day; the next morning (IST) up to 8
  are turned into 60-word notes and questions. Each must quote the release,
  and the verifier must answer every question by itself and pick the same
  option.
- The subject banks grow by up to 10 questions per subject per day. A static
  question is kept only if the verifier answers it correctly twice, with the
  options shuffled the second time.
- The daily quiz (10 questions: up to 4 current affairs, the rest across
  subjects) is written once per IST day and never repeats a question from the
  last 60 days.

## Watching it

`status.json` on the feed-data branch shows the last run: links found per
source (0 means the page changed or blocks the runner), notices published or
held for review, quiz counts, and DeepSeek calls and tokens.

## Adding a source

Add an entry to `sources.json` (`id`, `name`, `category`, `url`, `states`).
Run `python -m pytest -q` in this folder before pushing.

## Reports from the apps

"Report a mistake" in the apps writes to the Firebase database
(project dice-dhamaal, path `feedReports`). Review them in the Firebase console.
