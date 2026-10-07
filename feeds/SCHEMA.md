# Feed files (Vacancy Bell + Roz Quiz)

The pipeline in `feeds/` (GitHub Actions in this public repo) publishes JSON
files to the orphan branch `feed-data`. Apps read them from:

    https://raw.githubusercontent.com/hemantpatle153-ops/ad-apps-builds/feed-data/<path>

Every text a user sees is bilingual: `{"en": "...", "hi": "..."}` (`Text` below).
Dates are `YYYY-MM-DD` strings (India time), timestamps are ISO 8601 UTC.
Unknown values are `null`, never guessed. Only facts that passed the fact
check are published; a field that failed it is `null` and the app shows
"See official notice".

All files carry `"schema": 1`. Apps must ignore unknown keys.

## Vacancy Bell (jobs)

### `jobs/index.json`
```json
{
  "schema": 1,
  "generatedAt": "2026-10-07T14:30:00Z",
  "posts": [PostSummary, ...]          // newest first, at most 600
}
```

`PostSummary`:
```json
{
  "id": "ssc-cgl-2026-notice",          // stable, [a-z0-9-], unique
  "type": "job",                        // job | admit_card | result | answer_key | syllabus | admission | notice
  "title": Text,                        // "SSC CGL 2026 Recruitment"
  "org": Text,                          // "Staff Selection Commission"
  "source": "ssc",                      // source id, see Sources
  "category": "ssc",                    // ssc | railway | banking | upsc | defence | police | teaching | state_psc | psu | medical | engineering | other
  "postedAt": "2026-10-07T09:12:00Z",
  "updatedAt": "2026-10-07T09:12:00Z",
  "lastDate": "2026-11-05",             // null if unknown / not applicable
  "totalPosts": 14582,                  // null if unknown
  "qualifications": ["graduate"],       // any of: 8th 10th 12th iti diploma graduate postgraduate engineering medical law any
  "states": ["all"],                    // "all" or state codes: AN AP AR AS BR CH CG DN DL GA GJ HR HP JK JH KA KL LA LD MP MH MN ML MZ NL OD PY PB RJ SK TN TS TR UP UK WB
  "ageMin": 18, "ageMax": 32,           // null if unknown
  "salary": Text|null,                  // "₹25,500 – ₹1,51,100 (Level 4-8)"
  "tags": ["central-govt"],
  "verified": true
}
```

### `jobs/posts/<id>.json` (full post)
All `PostSummary` fields plus:
```json
{
  "shortInfo": Text,                                  // <= 60 words
  "importantDates": [{"label": Text, "date": "2026-11-05"|null, "text": "05 Nov 2026 (11 PM)"}],
  "fees": [{"category": Text, "amount": 100|null, "text": "₹100"}],
  "age": {"min": 18|null, "max": 32|null, "asOn": "2026-08-01"|null, "relaxation": Text|null},
  "vacancies": [{"post": Text, "total": 120|null, "breakdown": "UR 50, OBC 32, EWS 12, SC 18, ST 8"|null}],
  "eligibility": Text|null,
  "selection": [Text],                                 // ordered stages
  "howToApply": Text|null,
  "links": [{"label": Text, "url": "https://..."}],   // apply, official notice, admit card, result...
  "officialNoticeUrl": "https://...pdf"|null,
  "sourcePageUrl": "https://ssc.gov.in/...",
  "check": {"model": "deepseek-flash", "rounds": 2, "factsChecked": 31, "factsDropped": 1}
}
```

## Roz Quiz

### `quiz/index.json`
```json
{
  "schema": 1,
  "generatedAt": "...",
  "daily": ["2026-10-07", "2026-10-06", ...],          // newest first, last 60 days
  "currentAffairs": ["2026-10-07", ...],               // newest first, last 60 days
  "banks": [{"id": "polity", "title": Text, "count": 412, "file": "quiz/banks/polity.json", "updatedAt": "..."}]
}
```

### `Question`
```json
{
  "id": "q-polity-000123",             // stable, unique across all files
  "q": Text,
  "options": [Text, Text, Text, Text], // exactly 4
  "answer": 2,                         // index 0-3
  "explanation": Text,
  "subject": "polity",                 // gk history polity geography economy science current_affairs maths reasoning english computer
  "exams": ["ssc", "upsc"],            // ssc railway banking upsc state_psc defence teaching
  "difficulty": "medium",              // easy | medium | hard
  "source": {"name": "PIB", "url": "https://pib.gov.in/..."} | null,
  "asked": "SSC CGL 2023" | null,      // set only for real previous-year questions
  "verified": true
}
```

### `quiz/daily/<date>.json`
```json
{"schema": 1, "date": "2026-10-07", "title": Text, "questions": [Question x 10]}
```

### `quiz/current_affairs/<date>.json`
```json
{
  "schema": 1,
  "date": "2026-10-07",
  "notes": [{"id": "ca-2026-10-07-1", "title": Text, "body": Text, "source": {"name": "PIB", "url": "..."}, "tags": ["economy"]}],
  "questions": [Question, ...]
}
```

### `quiz/banks/<subject>.json`
```json
{"schema": 1, "subject": "polity", "questions": [Question, ...]}
```

## Reports ("Report a mistake")

Apps write to the shared Firebase Realtime Database (project `dice-dhamaal`)
after anonymous sign-in, at `feedReports/<push id>`:

```json
{"app": "vacancy_bell"|"roz_quiz", "item": "<post or question id>", "reason": "wrong_date"|"wrong_fee"|"wrong_eligibility"|"wrong_answer"|"wrong_question"|"broken_link"|"other", "note": "<= 300 chars", "by": "<auth uid>", "at": <server timestamp>}
```

Only new reports can be written; nobody can read them from an app.
Rahul reviews them in the Firebase console.
