# Day 1 — exact sequence

## Before Claude Code (no prompts spent, ~45 min)

1. AWS Builder Center — verify student status. This gates everything.
2. Pick the target repo. Candidates: `psf/requests`, `encode/httpx`, `Textualize/rich`,
   `pallets/click`. **Confirm its suite goes green locally before committing to it.**
   ```bash
   git clone <repo> /tmp/target && cd /tmp/target
   pip install -e ".[dev]" && pytest -x -q
   ```
   If that fails, pick the next candidate. Do not try to fix someone else's build.
3. `gh auth login`
4. Fork the target repo (you need it for the seeded branches).
5. Drop these files into `PR-review/`:
   ```
   CLAUDE.md
   SPEC.md
   .claude/skills/component/SKILL.md
   .claude/skills/characterization/SKILL.md
   ```
6. `config.toml`:
   ```toml
   [target]
   slug = "psf/requests"          # your pick
   fork = "<you>/requests"
   package_root = "src/requests"
   ```
7. Commit and push.

## Claude Code config

```bash
claude --model opusplan --effort high
```

Or in `.claude/settings.json`:
```json
{ "model": "opusplan", "effortLevel": "high", "alwaysThinkingEnabled": true }
```

## The prompt sequence

Each line is one prompt. `/clear` between components.

```
/component C0
/clear
/component C1
/clear
/component C2
/clear
/component C3
/clear
/component C4
```

That is Thursday. Five components, maybe 15–25 prompts with iteration.

**After C4 you have a complete, submittable project.** Stop and confirm that before
touching C5.

## Friday

```
/component C5
/clear
/component C6
/clear
/component C7        ← read the output yourself before continuing
/clear
/component C8
```

**Gate at C7.** Print the learned norms. If they read as generic ("write good code",
"follow conventions"), the enforcement filter in C6 is too loose — fix C6 before building
C8 on top of bad data.

**Hard stop Friday 9pm.** If C7 is not producing specific, citable norms by then, freeze
the brain at declarative-only and spend Saturday on C9–C11. Decide this now, not at 2am.

## Saturday (Bangalore, in person)

```
/component C9
/clear
/component C10
/clear
/component C11
```

Then seed the three branches and run end to end.

And talk to the Amazon engineers. You said connections were the point of going.

## Sunday

Record the video (budget 4 hours), writeup, submit by mid-afternoon, then polish.

## When something breaks

- Corrected the same thing twice → `/clear`, bring it to the design chat, come back with a
  better prompt.
- Claude wants to change a SPEC.md interface → it should stop and say so. If it changed one
  silently, revert and re-prompt; drift compounds.
- A test only passes with a mock → that is a failure, not a pass. The spec says real
  artifacts.
- Budget burning faster than expected → drop to `--model sonnet` for everything except plan
  mode.
