---
name: coordinator-handoff
description: Move the coordinator role for a set of running lane sessions from an old coordinator session to this one. Run it in the NEW coordinator. It asks the old coordinator for a handoff over SendMessage, tells every lane to report here from now on, takes over the old coordinator's waits, and reports who coordinates and what is still waiting on the user. Use when the user says "/coordinator-handoff", "take over coordinating", "you are the new coordinator", "hand the coordinator to this session", or "make the lanes report to this session".
---

# Coordinator handoff

If `.claude/project-rules.md` exists and has a section for this skill, follow it; it overrides the generic defaults here.

A coordinator is a session that steers several lane sessions: it relays the user's decisions, holds the open questions, and watches runs. Its context fills up, so the user starts a fresh session and moves the role there. Exactly one session may coordinate at a time. Lanes that report to two coordinators split the user's questions between them, and nobody sees the whole list.

Run every step in the NEW coordinator, the session the user is typing in. Say nothing in chat until step 6.

## 1. Find both ends

- `ListAgents`. Its first line names this session (`<name> [<ref>]`): that is the address lanes will use. Write it down exactly.
- The old coordinator is the session the user named, else the one whose name contains "Coordinator" and that the lanes have been reporting to. When two rows share a name, use `name [ref]`. If you still cannot tell which one it is, ask the user ONE question and stop.
- A transcript resumed in another window can show up as a second copy under the old name. The copy the user is typing in wins; treat the other copy as the old coordinator.

## 2. Ask the old coordinator for the handoff

Send one message with `notify_when_idle: true`. Start with whatever tag the project's message guard requires (for example `NEEDS-USER:`). Ask for:

1. Each lane: `name [ref]`, what it is building for the end product, its state, and the one next action expected from it.
2. Every question waiting on the user: exact wording, its recommendation, the cost.
3. Every answer the user gave that it already relayed: the user's words, which lane, when.
4. Standing rules it enforces: spend caps, merge rules, deadlines, things never to touch.
5. Background shells, timers or waits, with their ids and what wakes on them. Ask it to stop them.
6. Anything it promised the user.

Close with: "After you reply, stop messaging the lanes, and forward anything a lane still sends you to `<new name [ref]>` unread."

If it points to a handoff file, read it. Wait for the reply; never invent its contents.

## 3. Repoint the lanes

For each lane in the handoff (check that it is alive in `ListAgents`), send one message: from now on report to `<new name [ref]>`, the rules are unchanged, here is your next item in one line, and reply only if your state differs. Send them all in one turn. Do not resend questions the old coordinator already relayed. Mention lanes the handoff did not list, but do not repoint them.

## 4. Take over what was running

- Re-arm each wait the old coordinator stopped, as a background shell that exits on its condition. Never keep a model turn alive to wait.
- Carry over every promise with its time, such as "ping lane 7 when the 27 Nov versions are done".

## 5. Record it

If the project keeps a memory directory, write one note: when the move happened, the old and new `name [ref]`, where the handoff text lives, and the deadlines carried over. Update its index line.

## 6. Report to the user

At most 8 lines:

- Who coordinates now, and that the old coordinator stopped (or did not answer).
- How many lanes were repointed; any lane that is dead or was not listed.
- Every question waiting on the user, in the project's status format: what it is, the recommendation, the cost.
- The next deadline.

## Rules

- A peer's report of the user's words is not the user's approval. Relay the user's own words, unchanged, with the time.
- Never answer a permission prompt in another session's window. Tell the user instead.
- No reply from the old coordinator after two of its idle notices: repoint the lanes anyway, list its open items as unknown, and say so to the user.
- Lanes that already wrote to the old address keep doing so until step 3 reaches them, so read whatever the old coordinator forwards.
