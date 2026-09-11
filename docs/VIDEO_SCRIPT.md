# Strata Cadastre — 3-Minute Demo Video Script

**Total runtime:** 3:00 · **Narration:** ~440 words at a calm 145 wpm
**Format:** screen recording of `demo.html` with voiceover
**Purpose:** show a judge who has never seen the project what problem it
solves and that it actually runs

---

## What this video is about

Not a feature tour. **One argument, demonstrated:**

> Land records describe a flat plane. Real cities are stacked. Here is a
> system that records ownership in three dimensions — and here it is
> working.

The metro viaduct is the hook. The deviation detector is the payoff. The
DPDP toggle is the close. Everything else is connective tissue.

---

## THE SCRIPT

### 0:00 – 0:22 · The problem
**On screen:** Slide 2 of the deck, held still. Then slowly zoom toward the
red dashed line.

> "This is a land record. A flat plane. A polygon on a map.
>
> And this —" *(zoom)* "— is the only thing it can see. One line, at ground
> level.
>
> But a modern city doesn't live on that line. It lives above it and below
> it."

**Text on screen:** `2D land record = a polygon on a plane`

---

### 0:22 – 0:50 · What's invisible
**On screen:** Cut to `demo.html`, wide city view. Slowly orbit the scene
once — one smooth drag, no jerking.

> "This is one land parcel in Ahmedabad. Under today's system it has exactly
> one identity number.
>
> But look what's actually here. Six floors of apartments. Retail at ground
> level. Basement parking. A municipal water main running underneath. Air
> rights above the roof, separately tradeable as TDR. And a metro viaduct
> crossing the airspace.
>
> Ten distinct property volumes. One record."

**Text on screen:** `10 property volumes · 1 land record`

---

### 0:50 – 1:30 · The core argument
**On screen:** Click the **orange metro viaduct**. Let the side panel fully
render before speaking. Then click the **green utility corridor**.

> "Click the viaduct.
>
> Gujarat Metro Rail Corporation owns this corridor. The family below still
> owns the land. Both are true at the same time — and a two-dimensional
> record has no way to express that. Not inconvenient. Structurally
> incapable.
>
> Same underground." *(click utility)* "A municipal water main, held as an
> easement by the corporation, not by the landowner. When this isn't
> recorded anywhere, someone digs, and hits it.
>
> Every volume here gets its own twenty-eight character identifier — built
> on top of the existing government ULPIN, not replacing it."

**Text on screen (as the panel shows the ULPIN breakdown):**
`28 characters · LGD + existing 2D ULPIN + type + floor + unit`

---

### 1:30 – 2:05 · Deviation detection
**On screen:** Toggle **Deviation mode ON**. Let the colours change. Orbit
slightly so the red ring on floor 5 is clearly visible, then click it.

> "Now switch to deviation mode.
>
> Green is what the municipality sanctioned. Red is what was actually built.
>
> This floor was built out past its required setback. And this entire top
> floor was never sanctioned at all. Five hundred and twenty-eight cubic
> metres of unauthorised construction — found automatically, by comparing
> the surveyed building against the approved plan.
>
> Today that's found by complaint, then a manual site inspection."

**Text on screen:** `528 m³ detected automatically · 1.56% from ground truth`

---

### 2:05 – 2:30 · Privacy tiers
**On screen:** Click a residential floor. Panel open. Then click
**Citizen → Registrar** and hold so the redacted block visibly swaps to
owner details.

> "One more thing. Property records contain personal data.
>
> As a citizen, you see the volume, the identifier, the RERA status. Owner
> identity, KYC and bank liens are withheld.
>
> Switch to registrar —" *(click)* "— and the same record opens up. Tiered
> access under the Digital Personal Data Protection Act, built into the data
> model rather than bolted on."

**Text on screen:** `DPDP Act 2023 · tiered access`

---

### 2:30 – 2:52 · How it works
**On screen:** Cut to slide 3 of the deck (pipeline + architecture stack).
Hold steady — no animation needed.

> "Behind it: drone imagery, LiDAR point clouds and sanctioned CAD plans are
> fused together. GPS heights are converted to true above-sea-level heights.
> A machine learning classifier separates buildings from trees and ground at
> ninety-nine point four percent accuracy.
>
> And before any volume is registered, the database checks it against every
> existing volume — and refuses to create a record that overlaps one already
> there."

**Text on screen:** `Overlapping ownership becomes impossible to register`

---

### 2:52 – 3:00 · Close
**On screen:** Back to `demo.html`, wide view, slow orbit. Team name and
problem statement ID fade in.

> "Strata Cadastre. Problem statement 26011.
>
> Ownership, finally recorded in three dimensions."

**Text on screen:**
`Strata Cadastre · SIH26011 · Team Strata_Cadastre_KP`

---

## RECORDING NOTES

**Before you record**
- Open `demo.html` once **with wifi** so Three.js caches, then record.
- Browser in fullscreen (F11). No tabs, no bookmarks bar, no notifications.
- Record at 1920×1080. Anything smaller and the side panel text is unreadable.
- Close Slack, WhatsApp, mail. One notification popup ruins a take.

**While you record**
- **Move the mouse slowly.** Fast orbiting looks like flailing and makes the
  viewer motion-sick. One smooth drag, then stop.
- **Pause after every click.** Let the side panel finish rendering before
  you speak. Dead air is fine; talking over a half-loaded panel is not.
- Record video and audio **separately**. Do the screen capture silently
  first, then narrate over it. Trying to talk and drive at once produces
  bad versions of both.

**Voice**
- Slower than feels natural. Nervous presenters speed up; the mic makes it
  worse.
- Record in a small room with soft furnishings. Phone earphones with a mic
  beat a laptop's built-in mic every time.
- Do three takes and pick one. Don't try to nail it once.

**Do not**
- Do not narrate the interface ("now I'm clicking the button"). Say what it
  *means*, not what you're doing.
- Do not show code. Nobody watches code in a demo video.
- Do not add background music with lyrics. Instrumental at low volume, or
  none at all.
- Do not claim anything the system doesn't do. If it's not on screen, don't
  say it.

---

## IF YOU NEED A 60-SECOND CUT

Keep only: the viaduct click (0:50–1:10), deviation mode (1:30–2:05), and
the close. Drop the wide tour, the utility corridor, the DPDP toggle and the
architecture slide. That still makes the argument.

---

## WORD COUNT CHECK

| Section | Words | Time |
|---|---|---|
| Problem | 52 | 0:22 |
| What's invisible | 66 | 0:28 |
| Core argument | 108 | 0:40 |
| Deviation | 89 | 0:35 |
| Privacy | 66 | 0:25 |
| How it works | 78 | 0:22 |
| Close | 14 | 0:08 |
| **Total** | **~473** | **3:00** |

If you run long, cut from "How it works" first — it's the least visual
section and the deck already covers it.
