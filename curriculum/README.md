# Curriculum

One syllabus per language (`es.yaml`, `fr.yaml`, `de.yaml`): what a learner meets on the way from
A1 to B2, in teaching order within each level, plus topic cards the mentor can tell. Faria reads
them at the start of each session (course/tracker.py, ADR-034): the mentor is offered one or two
items worth introducing, words due for review and a couple of stories, as suggestions, never an
agenda. Coverage is tracked from what the learner actually says. Items marked `cut` are never
offered.

Edit them in the Helm (Faria's **Syllabus** tab) or by hand; both keep the same format.

## Items

```yaml
- id: es.a2.preterite-regular   # <language>.<level>.<slug>; never reused, so coverage evidence can point at it
  level: A2                     # A1 | A2 | B1 | B2
  strand: grammar               # grammar | function | vocab | topic
  title: "Preterite: regular verbs"
  notes: "What to cover, and what English speakers get wrong."
  example: "Ayer comí paella con mis amigos."
  english: "Yesterday I ate paella with my friends."
  tidbit: "A true, interesting bit of history, art, literature or philosophy tied to this item."
  words: [...]                  # vocab items: the core words of the theme (topics: optional)
  facts:                        # topic items only: what the mentor may say, and nothing beyond it
    - "One checked fact per line."
  status: draft                 # draft (Claude's) | checked (yours) | cut (kept for the record, not taught)
```

- **grammar**: a structure to be introduced, practised and made solid.
- **function**: a CEFR-style "can do" (tell a story, disagree politely), the situations the
  tutor steers toward so the grammar gets used for something.
- **vocab**: a theme with its core words. Words the mentor actually teaches enter spaced review
  (Leitner boxes, reviewed in conversation, never as flashcards).
- **topic**: a story or cultural note (history, etymology, regional varieties) the mentor can
  tell when the conversation goes there. `notes` says when it fits; `facts` are the only claims
  it should make, so every fact should be one you'd stand behind.

Every item has a **tidbit**: what the mentor tells when the learner has nothing to talk about, or
when the item comes up. Like `facts`, it's the only claim the mentor should make, so it has to be
one you'd stand behind.

Order matters: within a level, items are listed in the order they should be introduced.

## Sources

Drafted by Claude from general knowledge of how these languages are usually sequenced, in the
spirit of the CEFR Companion Volume (Council of Europe, 2020), the Instituto Cervantes' *Plan
Curricular* (Spanish), the *Référentiels* for French and *Profile deutsch* / the Goethe-Institut
word lists (German). Nothing is copied from them; every item is `draft` until checked by hand.
