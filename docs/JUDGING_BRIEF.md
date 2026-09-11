# Strata Cadastre — Judging Briefing
### What to say, slide by slide, and what they'll ask

Assume nothing. This document explains what you built, in the order you'll
present it, plus the questions judges actually ask and how to answer them.

---

## PART 0 — The one sentence

If you only get one line out before someone interrupts:

> **"Indian land records identify plots of ground. We built a system that
> gives every apartment, basement, utility line and air-rights volume its
> own unique 3D identity — so ownership above and below the ground can
> finally be recorded."**

Everything else is elaboration.

---

## PART 1 — What you actually built (know this cold)

Six pieces. You should be able to name all six without notes.

| # | Piece | What it does |
|---|---|---|
| 1 | **`geodesy.py`** | Converts GPS heights into real above-sea-level heights (H = h − N) |
| 2 | **`pipeline.py`** | Fuses drone/LiDAR/CAD data, validates topology, detects illegal construction |
| 3 | **`ml_classifier.py`** | Trained ML model that separates buildings from trees and ground |
| 4 | **`floorplan_extract.py`** | Reads a floor plan image and extracts rooms as measured polygons |
| 5 | **`main.py` + `schema_3d_cadastre.sql`** | Generates 3D-ULPINs; database that physically rejects overlapping volumes |
| 6 | **`demo.html`** | The 3D viewer judges will actually look at |

### The three numbers that matter most

- **99.39%** — ML classifier accuracy on held-out test data
- **528 m³** — unauthorised construction the system detected automatically
- **1.56%** — how close that detection was to the mathematically correct answer

### The 3D-ULPIN itself

28 characters, five parts:

```
240124  11000088421000  T  906  T010
LGD     existing 2D     │  floor unit
code    ULPIN           └─ vertical type
```

Six vertical types: **B**asement, **G**round, **F**loor, **T**errace/air-rights,
**U**tility, **E**levated corridor.

**Critical framing:** the middle 14 characters are the *existing* government
ULPIN. You are not replacing anything. You are extending it.

---

## PART 2 — Slide by slide

### SLIDE 1 — Title
**On it:** Project name, PS ID 26011, theme, team, and a panel showing the
six vertical types as coloured badges.

**Say (15 seconds):**
> "We're team Strata_Cadastre_KP on problem statement 26011 — 3D ULPIN
> generation. Our system takes one land parcel and gives separate identity
> to six kinds of vertical property: basement, ground, floors, air rights,
> underground utilities, and elevated corridors."

Point at the red line: *"A conventional land record can only express the
first one."*

---

### SLIDE 2 — Proposed Solution
**On it:** Three dark cards on the left (the gap / why it matters / our
solution). Centre: a building drawn as a stack of coloured volumes with a
**red dashed line** through it. Right: six red-vs-green comparison rows.

**This is your most important slide. Slow down here.**

**Say (60–75 seconds):**
> "Today, land records identify parcels of ground. A twelve-storey building
> is one parcel — twenty-four flats share a single ULPIN.
>
> *[point at the red dashed line]*
> This dashed line is everything a 2D land record can see. Look what sits
> above and below it.
>
> *[point up]* Air rights — separately tradeable as TDR in Gujarat.
> *[point at the orange bar]* A metro viaduct crossing the airspace. The
> family below still owns the land; the Metro Corporation owns that volume.
> *[point down]* A municipal water main under the plot, held as an easement
> by AMC, not by the landowner.
>
> A 2D record has no field for any of this. Not inconvenient — structurally
> incapable. It's a polygon on a plane.
>
> We give each volume its own 28-character identifier, built on top of the
> existing 14-digit ULPIN, and the database refuses to register a volume
> that overlaps one already there."

**Lead with the viaduct and the utility line, not the flats.** Flats have
other identifiers — property tax numbers, society share certificates. The
subsurface and airspace case is the one with no counter-argument.

---

### SLIDE 3 — Technical Approach
**On it:** Six coloured arrows (the pipeline), a layered architecture stack,
a cloud with your links, and validated metrics along the bottom.

**Say (45 seconds):**
> "Six stages. We ingest drone imagery, LiDAR point clouds, sanctioned CAD
> plans and GPS coordinates. We convert GPS heights into proper
> above-sea-level heights. Machine learning separates building points from
> trees and ground. Topology validation rejects any overlap. Then each
> volume gets its ULPIN, and it's served to a 3D viewer with QR
> verification.
>
> The numbers along the bottom are measured output from our own test suite,
> not estimates."

If asked to pick one stage to expand: **topology validation** — it's the
part that actually prevents the harm.

---

### SLIDE 4 — Feasibility and Viability
**On it:** Three columns — feasibility, risks, mitigation — five points
each, and a strip at the bottom saying what is and isn't validated.

**Say (30 seconds):**
> "Every risk here has a mitigation directly opposite it. The biggest one:
> free high-resolution LiDAR doesn't exist for Ahmedabad. Our answer is
> phone photogrammetry — a couple of hundred overlapping photos processed
> into a real point cloud. No drone permit, no cost.
>
> And the bottom strip states plainly what we've validated versus what we
> haven't. Our ML is trained on synthetic data, not field-labelled LiDAR.
> We're telling you that before you ask."

**That last sentence is worth more than any feature claim.** Judges are used
to teams overclaiming. Volunteering a limitation reads as competence.

---

### SLIDE 5 — Impact and Benefits
**On it:** Four numbers across the top, two charts, space for your photos,
four beneficiary groups, three benefit categories.

**Say (30 seconds):**
> "The left chart is the core of it — the same physical parcel holds one
> record under the current system and ten distinct ownership volumes under
> ours.
>
> The right chart is our deviation detector on a test building: it found a
> terrace built past its setback and an entire unauthorised floor, 528 cubic
> metres, automatically. Today that's found by complaint and manual
> inspection."

---

### SLIDE 6 — Research and References
**On it:** Standards on the left, government sources on the right, and a
strip at the bottom for your field research.

**Say (15 seconds):**
> "We're built on ISO 19152, the international land administration standard,
> and OGC CityGML. And we went to the field — the City Mamlatdar's office
> for dispute process, City Survey for the Property Card format, and a local
> developer for sanctioned plans."

**The field research line is a differentiator.** Almost nobody at an
internal round will have talked to an actual government office.

---

## PART 3 — The demo (4 minutes, rehearse it)

Open `demo.html`. **Load it once with wifi first** so Three.js caches, then
it works offline.

1. **Open on the wide city view.**
   > "One parcel. Ten separate property volumes."

2. **Click the metro viaduct** (orange bar crossing).
   > "GMRC owns this corridor. The family below owns the land. A 2D record
   > cannot hold both facts at once."

3. **Click the utility corridor** (green, underground).
   > "Municipal water main, held as an easement. When this isn't recorded,
   > someone digs and hits it."

4. **Turn on Deviation mode.**
   > "Green is sanctioned. Red is not. 528 cubic metres, found automatically."

5. **Switch Citizen → Registrar.**
   > "Same record, two access tiers. Owner name, KYC and bank lien appear
   > only for the registrar. That's DPDP Act compliance built into the data
   > model."

**End on the tier toggle.** It's your most quotable ten seconds.

---

## PART 4 — Questions they will ask

### Q: "Why do we need this? Flats already have property tax numbers."
This is the sharpest question and it's fair.

> "They do — but those are administrative identifiers, not spatial ones. A
> tenement number can't tell you that flat 402's terrace extension overhangs
> flat 401's balcony, because it holds no geometry. And it does nothing at
> all for the metro tunnel or the water main, which is where our strongest
> case is."

### Q: "Is 3D better than 2D? Should we replace 2D ULPIN?"
**Do not say yes.**

> "No. Most Indian parcels are agricultural or single-storey, and for those
> a 2D polygon is the correct representation — adding a third dimension is
> pure cost. This is an extension for dense urban parcels and infrastructure
> corridors. Our database literally hangs 3D units off the existing 2D
> parcel record."

### Q: "Where is the AI/ML? This looks like geometry."
> "Random Forest classifier, scikit-learn, eight geometric features per
> point — height above local ground, planarity, linearity, sphericity,
> verticality, density and height variance. 99.39% held-out accuracy. It
> separates trees from buildings, which matters because vegetation
> misclassified as building inflates every volume downstream. A simple
> height threshold can't do that — both are tall."

### Q: "How do you know it works?"
**This is your best question. Have this ready.**

> "We wrote an automated verification suite for our own database — and it
> caught a units bug in our schema. Volumes were being computed in degrees
> instead of metres, wrong by ten orders of magnitude, and it never threw an
> error. We fixed it and added a permanent check so it can't come back."

That's a testing story. Very few teams will have one.

### Q: "Did you use real data?"
Answer honestly and immediately:

> "No — our ML is trained on synthetic point clouds we generate, because
> free labelled LiDAR for Ahmedabad doesn't exist. The model, the pipeline
> and the metrics are real; the data is simulated. We've been to a local
> developer to source real sanctioned plans, and phone photogrammetry gives
> us a path to a real point cloud without a drone permit."

Never bluff this. If they catch you, everything else you said becomes
suspect.

### Q: "How does it scale to a whole city?"
> "Point clouds are streamed as COPC/EPT, never stored in the database —
> only vectorised volumes are. Spatial indexes on the geometry. For
> rendering, 3D Tiles is the next step; right now we render entities, which
> is correct at single-parcel scale."

### Q: "What stops two people owning the same space?"
> "A database trigger. Before any volume is written, it's tested against
> every registered volume on the same floor of the same parcel. Genuine
> overlap raises an exception and the record is never created. And it
> correctly distinguishes a shared party wall — which is normal — from
> actual volumetric overlap."

### Q: "Who would use this? Is it deployable?"
> "It's self-hosted Docker — database, API and web server on one machine.
> That matters because government land records run in NIC and state data
> centres, not on external cloud."

### Q: "What's your biggest weakness?"
Pick one and own it:

> "The legal framework. We can build the spatial record perfectly, but if no
> statute requires the Sub-Registrar to check it before registering a deed,
> nothing changes. The Sub-Registrar registers documents; he doesn't verify
> title. That's a legislative change, not a software one — and countries
> that have done this, like the Netherlands and Queensland, legislated
> strata title first and built the geometry after."

Judges respect a team that has thought about why their own project might
fail.

### Q: "How long did this take / who did what?"
Have the split ready before you walk in. Don't work it out on stage.

---

## PART 5 — Rules for the room

**When you don't know:** *"I don't know — I'd have to check."* Then move on.
Never invent a number. A judge who catches one fabricated figure discounts
every other figure you gave.

**Don't say "it's fully working"** — say what's working and what isn't. You
have a genuinely strong position; overclaiming is the only way to lose it.

**Every number you quote must be one of ours:** 99.39%, 528 m³, 1.56%,
±1 cm, 0.022°, 28 characters, 6 types, 10 volumes. Don't quote national
statistics you haven't verified.

**If the demo breaks:** don't debug on stage. Switch to the backup video or
the slides and keep talking. Fumbling silently costs more than the demo was
worth.

**Decide who answers what before you go in.** One person leads the demo, one
handles technical questions, one handles the legal/policy side. Nothing
looks worse than three people starting to answer at once.

---

## PART 6 — Last-hour checklist

- [ ] `demo.html` opened once with wifi, then tested with wifi **off**
- [ ] Backup video recorded and on the laptop, not in the cloud
- [ ] Deck exported to **PDF**
- [ ] Team ID filled in on slide 1
- [ ] Links pasted into the cloud on slide 3
- [ ] Photos dropped into the space on slide 5
- [ ] Demo click-path rehearsed out loud, twice
- [ ] Each person knows which questions they take
