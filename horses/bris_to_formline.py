#!/usr/bin/env python3
"""
bris_to_formline.py — turn BRISnet single-file past-performance files (.DRF)
into the JSON that Form Line consumes.

    python3 bris_to_formline.py DMR0809.DRF [more.DRF ...] -o data/

One .DRF holds one track's full card. Pass several to build a multi-track day;
they are grouped into one file per race date.

Field numbers below are 1-indexed to match BRISnet's published layout at
https://support.brisnet.com/hc/en-us/articles/360056092092
"""

import argparse, csv, json, os, sys
from collections import defaultdict

YARDS_PER_FURLONG = 220

# --- 1-indexed field map -----------------------------------------------------
TRACK, RDATE, RACE_NO, POST_POS, ENTRY = 1, 2, 3, 4, 5
DIST_YDS, SURFACE, RACE_TYPE = 6, 7, 9
CLASS, PURSE = 11, 12
TRAINER, JOCKEY = 28, 33
SILKS, PROGRAM, ML_ODDS = 40, 43, 44
HORSE, YOB, SEX, WEIGHT = 45, 46, 49, 51
DIST_STARTS, DIST_WINS = 65, 66          # lifetime at today's distance
TRK_STARTS,  TRK_WINS  = 70, 71          # lifetime at today's track
LIFE_STARTS, LIFE_WINS = 97, 98
PP_DATE, PP_TRACK, PP_DIST, PP_SURF, PP_FINISH = 256, 286, 316, 326, 616
DAYS_OFF, POST_TIME, EQB_COND = 224, 1418, 1429
PP_N = 10                                 # BRIS carries 10 prior starts
FLIP = True                               # surname-first -> first-last for people

RACE_TYPES = {
    'G1':'Grade 1', 'G2':'Grade 2', 'G3':'Grade 3', 'N':'Stakes',
    'A':'Allowance', 'R':'Starter Allowance', 'T':'Starter Handicap',
    'C':'Claiming', 'CO':'Optional Claiming', 'S':'Maiden Special Weight',
    'M':'Maiden Claiming', 'AO':'Allowance Opt Clm', 'MO':'Maiden Opt Clm',
    'NO':'Optional Claiming Stakes',
}
SURFACES = {
    'D':'Dirt', 'T':'Turf', 'd':'Inner dirt', 't':'Inner turf',
    's':'Steeplechase', 'h':'Hunt', 'A':'All-weather',
}


def furlongs(yards_field):
    """BRIS gives yards; a negative value flags an 'about' distance."""
    try:
        y = int(str(yards_field).strip())
    except (TypeError, ValueError):
        return None, False
    if y == 0:
        return None, False
    return round(abs(y) / YARDS_PER_FURLONG, 2), y < 0


def iso(d):
    d = str(d).strip()
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else None


def post_time(mil):
    """Field 1418 is Pacific military time, e.g. '1400'."""
    mil = str(mil).strip()
    return f"{mil[:2]}:{mil[2:]}" if len(mil) == 4 and mil.isdigit() else ''


# Display names for the BRIS track codes. Matching is always done on the code,
# never the name, so an unlisted track just shows its code and still scores.
TRACK_NAMES = {
    'DMR':'Del Mar', 'SA':'Santa Anita', 'LRC':'Los Alamitos', 'GG':'Golden Gate',
    'SAR':'Saratoga', 'BEL':'Belmont Park', 'AQU':'Aqueduct', 'BAQ':'Belmont at Aqueduct',
    'CD':'Churchill Downs', 'KEE':'Keeneland', 'ELP':'Ellis Park', 'TP':'Turfway Park',
    'GP':'Gulfstream Park', 'TAM':'Tampa Bay Downs', 'OP':'Oaklawn Park', 'FG':'Fair Grounds',
    'WO':'Woodbine', 'PIM':'Pimlico', 'LRL':'Laurel Park', 'MTH':'Monmouth Park',
    'PRX':'Parx Racing', 'PEN':'Penn National', 'DEL':'Delaware Park', 'CT':'Charles Town',
    'LS':'Lone Star Park', 'RP':'Remington Park', 'HOU':'Sam Houston', 'EMD':'Emerald Downs',
    'CBY':'Canterbury Park', 'PRM':'Prairie Meadows', 'HAW':'Hawthorne', 'AP':'Arlington',
    'IND':'Indiana Grand', 'TDN':'Thistledown', 'BTP':'Belterra Park', 'MVR':'Mahoning Valley',
    'DED':'Delta Downs', 'EVD':'Evangeline Downs', 'LAD':'Louisiana Downs', 'FL':'Finger Lakes',
    'TUP':'Turf Paradise', 'SUN':'Sunland Park', 'ZIA':'Zia Park', 'ALB':'Albuquerque',
    'ARP':'Arapahoe Park', 'WRD':'Will Rogers Downs', 'FON':'Fonner Park', 'MNR':'Mountaineer',
    'CLS':'Columbus', 'GPW':'Gulfstream West', 'SUF':'Suffolk Downs', 'ASD':'Assiniboia Downs',
}

SUFFIXES = {'JR', 'SR', 'II', 'III', 'IV'}
SMALL = {'of', 'the', 'and', 'a', 'de', 'du', 'la', 'le', 'von', 'van'}


def smart_title(s):
    """BRIS ships names in caps. Title-case them without mangling
    apostrophes ("HELEN'S TIME" -> "Helen's Time"), hyphens, or suffixes."""
    s = str(s).strip()
    if not s:
        return ''
    words = []
    for i, w in enumerate(s.split()):
        if w.upper().rstrip('.') in SUFFIXES:
            words.append(w.upper().rstrip('.'))
            continue
        if i and w.lower() in SMALL:
            words.append(w.lower())
            continue
        # capitalise each hyphen/apostrophe-joined part, but leave the letter
        # after a possessive apostrophe alone: O'BRIEN -> O'Brien, not O'brien
        out, cap = '', True
        for j, ch in enumerate(w):
            if cap and ch.isalpha():
                out, cap = out + ch.upper(), False
            elif ch in "-/":
                out, cap = out + ch, True
            elif ch == "'":
                # capitalise after apostrophe only for name prefixes (O', D')
                out, cap = out + ch, j <= 1
            else:
                out += ch.lower()
        if len(out) > 2 and out.startswith('Mc') and out[2].isalpha():
            out = 'Mc' + out[2].upper() + out[3:]
        words.append(out)
    return ' '.join(words)


def flip_name(s):
    """BRIS writes people surname-first with no comma ('ROSARIO JOEL').
    Two-token names flip cleanly; longer ones are ambiguous
    ('GLENN JAMES W JR') so they are left alone."""
    parts = s.split()
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 else s


def to_int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def finish_position(v):
    """Numeric finish, or the raw code for a non-completion (K, Q, X...)."""
    v = str(v).strip()
    return int(v) if v.isdigit() else (v or None)


def parse_file(path):
    with open(path, newline='', encoding='latin-1') as fh:
        rows = [r for r in csv.reader(fh) if len(r) > 100]

    if not rows:
        raise SystemExit(f"{path}: no usable rows — is this the single-file (DRF) format?")

    def g(row, n):
        return row[n - 1].strip() if n - 1 < len(row) else ''

    races = {}
    for row in rows:
        if g(row, ENTRY).upper() == 'S':          # scratched
            continue

        rno = to_int(g(row, RACE_NO))
        track, rdate = g(row, TRACK), iso(g(row, RDATE))
        dist, about = furlongs(g(row, DIST_YDS))
        surf = g(row, SURFACE)
        key = (track, rdate, rno)

        if key not in races:
            rtype = RACE_TYPES.get(g(row, RACE_TYPE), g(row, RACE_TYPE))
            races[key] = {
                'id': f"{track}-{rdate}-{rno}",
                'course': track,
                'courseName': TRACK_NAMES.get(track, track),
                'raceNumber': rno,
                'dist': dist,
                'about': about,
                'surface': SURFACES.get(surf, surf),
                'surfaceCode': surf,
                'going': SURFACES.get(surf, surf),
                'time': post_time(g(row, POST_TIME)),
                'name': g(row, CLASS) or g(row, EQB_COND) or rtype,
                'raceType': rtype,
                'purse': to_int(g(row, PURSE)),
                'runners': [],
            }

        # --- last 10 starts ---
        form = []
        for i in range(PP_N):
            d = iso(g(row, PP_DATE + i))
            if not d:
                continue
            pdist, _ = furlongs(g(row, PP_DIST + i))
            form.append({
                'pos':     finish_position(g(row, PP_FINISH + i)),
                'course':  g(row, PP_TRACK + i),
                'courseName': TRACK_NAMES.get(g(row, PP_TRACK + i), g(row, PP_TRACK + i)),
                'dist':    pdist,
                'surface': g(row, PP_SURF + i),
                'date':    d,
            })
        form.sort(key=lambda r: r['date'], reverse=True)

        # --- career flags ---
        # Fields 65-74 are lifetime AND surface-restricted (verified against the
        # PP block: assuming track-only or distance-only produces contradictions,
        # assuming track+surface / distance+surface produces none). So these
        # already answer "has it won here, on this surface" for the whole career.
        won_course = to_int(g(row, TRK_WINS)) > 0
        won_dist   = to_int(g(row, DIST_WINS)) > 0

        # The combined course-AND-distance win can only be proven from the 10-race
        # PP block, so it is capped at 10 starts. Where both lifetime flags are set
        # but no single PP run shows it, we cannot tell — recorded as None so the
        # scorer can decide rather than silently guessing.
        cd_seen = any(
            r['pos'] == 1 and r['course'] == track
            and r['dist'] == dist and r['surface'] == surf
            for r in form
        )
        won_cd = True if cd_seen else (None if (won_course and won_dist) else False)

        races[key]['runners'].append({
            'cloth':   to_int(g(row, PROGRAM), None) or g(row, PROGRAM) or to_int(g(row, POST_POS)),
            'name':    smart_title(g(row, HORSE)),
            'jockey':  flip_name(smart_title(g(row, JOCKEY))) if FLIP else smart_title(g(row, JOCKEY)),
            'trainer': flip_name(smart_title(g(row, TRAINER))) if FLIP else smart_title(g(row, TRAINER)),
            'age':     (2000 + to_int(g(row, YOB))) and None,   # replaced below
            'sex':     g(row, SEX),
            'weight':  to_int(g(row, WEIGHT)),
            'mlOdds':  g(row, ML_ODDS),
            'silksText': g(row, SILKS),
            'daysOff': to_int(g(row, DAYS_OFF), None),
            'record': {
                'starts': to_int(g(row, LIFE_STARTS)), 'wins': to_int(g(row, LIFE_WINS)),
                'courseStarts': to_int(g(row, TRK_STARTS)), 'courseWins': to_int(g(row, TRK_WINS)),
                'distStarts': to_int(g(row, DIST_STARTS)), 'distWins': to_int(g(row, DIST_WINS)),
            },
            'careerFlags': {'course': won_course, 'distance': won_dist, 'cd': won_cd},
            'form': form,
        })

        # age from year of birth against the race year
        yob = to_int(g(row, YOB), None)
        if yob is not None and rdate:
            yob += 2000 if yob < 50 else 1900
            races[key]['runners'][-1]['age'] = int(rdate[:4]) - yob
        else:
            races[key]['runners'][-1]['age'] = None

    for r in races.values():
        r['runners'].sort(key=lambda x: str(x['cloth']).zfill(3))
    return races


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('files', nargs='+', help='one or more BRIS .DRF files')
    ap.add_argument('-o', '--outdir', default='data', help='output directory (default: data)')
    ap.add_argument('--keep-name-order', action='store_true',
                    help="leave people's names surname-first, as BRIS supplies them")
    args = ap.parse_args()
    globals()['FLIP'] = not args.keep_name_order

    by_date = defaultdict(dict)
    for path in args.files:
        for (track, rdate, rno), race in parse_file(path).items():
            by_date[rdate].setdefault(track, []).append(race)

    os.makedirs(args.outdir, exist_ok=True)
    for rdate, tracks in by_date.items():
        meetings = []
        for track, races in sorted(tracks.items()):
            races.sort(key=lambda r: r['raceNumber'])
            meetings.append({
                'course': track,
                'courseName': TRACK_NAMES.get(track, track),
                'going': f"{len({r['surfaceCode'] for r in races})} surface"
                         + ('s' if len({r['surfaceCode'] for r in races}) > 1 else ''),
                'races': races,
            })
        out = os.path.join(args.outdir, f'racecards-{rdate}.json')
        with open(out, 'w') as fh:
            json.dump(meetings, fh, separators=(',', ':'))
        n_r = sum(len(m['races']) for m in meetings)
        n_h = sum(len(r['runners']) for m in meetings for r in m['races'])
        print(f"{out}  {len(meetings)} track(s), {n_r} races, {n_h} runners "
              f"({os.path.getsize(out)/1024:.0f} KB)")


if __name__ == '__main__':
    main()
