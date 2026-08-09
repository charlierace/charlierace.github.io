# horses

Form·Line — a horse racing form scorer. Drop this folder at the root of a repo
and GitHub Pages serves it at `https://<user>.github.io/<repo>/horses/`.

```
horses/
├── index.html                      the app (self-contained, no build step)
├── bris_to_formline.py             BRISnet .DRF  ->  Form Line JSON
└── data/
    ├── DMR0809.DRF                 sample input: Del Mar, 9 Aug 2026
    └── racecards-2026-08-09.json   the converted card
```

## Running it

`index.html` opens in a browser with no server — Sunday's Del Mar card is
embedded in it, so it works straight from the filesystem.

For the deployed version, switch **Scoring → Data** to
`./data/racecards-DATE.json` and it fetches instead. To make that the only
mode, delete the `<script id="embeddedCard">` block near the top of the
`<script>` section; the app falls back to fetching automatically.

## Adding a card

```bash
python3 bris_to_formline.py DMR0810.DRF -o data/
python3 bris_to_formline.py DMR0810.DRF SAR0810.DRF -o data/   # multi-track day
```

One output file per race date, named `racecards-YYYY-MM-DD.json`. Options:
`--keep-name-order` leaves people's names surname-first as BRIS supplies them.

## The scoring

Each runner's last 5 starts: 5 points for a win, 4 for second, down to 1 for
fifth. Sixth and worse, and any non-completion, score nothing. Then +2 for a
previous win at the course, +2 for a win over the distance, and +1 more if a
single win did both. Maximum 30.

All of it is adjustable in the **Scoring** drawer — runs counted, whether
course/distance wins come from the whole career or only the counted runs, and
how close a distance has to be to count as a match.

## Notes on BRIS data

- Fields 65–74 (lifetime record at today's distance / track) are **surface
  restricted**, despite the spec's wording. Verified against the past-performance
  block: assuming track-only gives contradictions, track+surface gives none. So
  Del Mar turf and Del Mar dirt wins are already kept apart.
- Those fields feed the +2 course and +2 distance bonuses directly, career-wide.
- The +1 for a single course-and-distance win can only be proven from the
  10-start past-performance block. Where both lifetime flags are set but no
  visible start shows one win doing both, the C&D chip reads `?` and the point
  is withheld by default (changeable in Scoring).
- Distances arrive in yards and are converted to furlongs; a negative value in
  the file means an "about" distance and is flagged rather than rounded away.

## Caveat

There's no weight, class, going, trainer form, pace or days-since-run in this
model, and it can't rank a first-time starter at all — see race 2 on the sample
card, where five debutantes tie on zero. It's a filter for shortening a card,
not an edge.
