# Ground rules

These rules apply to all work in this repo, by people and by coding agents.

## Writing: ASD-STE100

Write docs, code comments, commit messages, UI text and agent prompts in ASD-STE100
Simplified Technical English.

- Write short sentences: 20 words maximum for an instruction, 25 words maximum for a
  description.
- Write one instruction or one idea in each sentence.
- Use the active voice. Write instructions in the imperative: "Run the tests", not "The tests
  should be run".
- Use one word for one meaning. Use the same term for the same thing in all text.
- Use simple, common words. Do not use idioms, slang or unnecessary phrasal verbs.
- Do not remove articles ("the", "a") or other words to make a sentence shorter.
- Use lists or tables for steps and options.

## Plan execution

- Execute implementation plans inline, yourself, in the current session.
- Do not use subagent-driven or other multi-agent execution. It takes too long and uses
  unnecessary tokens.

## Git

- Never commit directly to `main` unless asked. Work on a branch.
- Commit only. Never push; the maintainer pushes.
- Make small, logical commits.
- Never amend or otherwise rewrite commits that may already be pushed. Add a new commit
  instead.
- Keep commit messages short: one imperative line, such as
  `Separate diagram node details from page navigation`. Add a body only when the change needs
  a short note.

## Pull requests

- Title: one short imperative line, like a commit message.
- Description: a few short, plain sentences or bullets saying what changed and
  how it was verified. See #54 and #56. No long narrative.

## UI controls

- Do not use native `select`, checkbox, radio, date or time pickers. Their open state ignores the theme.
- Use `web/src/components/ui/`. `web/src/test/native-controls.test.ts` checks this.
- Check each open state, such as a dropdown list, in dark mode.
