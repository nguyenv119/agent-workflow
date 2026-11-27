---
name: coordinator-handoff
description: Move the coordinator role for a set of running lane sessions between this session and another session the user names (`/coordinator-handoff <other session name>`). If this session coordinates now, it GIVES the role to the named session: it sends the full handoff and repoints every lane there. If not, it TAKES the role from the named session: it asks for the handoff and repoints every lane here. Either way it moves the waits, keeps one coordinator at a time, and reports. Use when the user says "/coordinator-handoff", "hand the coordinator to X", "give the handoff to X", "take over coordinating from X", "you are the new coordinator", or "make the lanes report to X".
argument-hint: <other session name, e.g. "Coordinator #2">
---

# Coordinator handoff

If `.claude/project-rules.md` exists and has a section for this skill, follow it; it overrides the generic defaults here.

A coordinator is a session that steers several lane sessions: it relays the user's decisions, holds the open questions, and watches runs. Its context fills up, or the user moves to another window, so the role moves to another session. Exactly one session may coordinate at a time. Lanes that report to two coordinators split the user's questions between them, and nobody sees the whole list.

The argument is the OTHER session's name. The user always names it; ask one question only if `ListAgents` shows no match.

## 1. Find both ends and the direction

- `ListAgents`. Its first line names this session (`<name> [<ref>]`). Find the other session from the argument. When two rows share a name, use `name [ref]`.
- **Give** when this session coordinates now: lanes report here, or the user says "give", "hand to", "pass to". **Take** otherwise: the user says "take over", "you are the new coordinator", or this session holds no lanes.
- A transcript resumed in another window can show up as a second copy under an old name. The copy the user is typing in is this session.

## 2a. Give: send the handoff

Send ONE message to the other session. Start with the tag the project's message guard requires (for example `NEEDS-USER:`). Write it so a session with no memory of this conversation can coordinate from it alone:

1. Each lane: `name [ref]`, what it is building for the end product, its state, and the one next action expected from it.
2. Every question waiting on the user: exact wording, the recommendation, the cost.
3. Every answer the user gave and where it was relayed: the user's words, which lane, when.
4. Any message from the user this turn that the other session must answer or act on: paste it word for word, and say "answer this in your window, not here".
5. Standing rules: spend caps, merge rules, deadlines, things never to touch, the user's status format.
6. Background shells, timers or waits, with ids: stop yours and say which the other session must re-arm.
7. Anything promised to the user.

Then go to step 3, with the other session as the new address. After that, stop messaging lanes, and forward anything a lane still sends here to the new coordinator unread.

## 2b. Take: ask for the handoff

Send one message to the other session with `notify_when_idle: true`, asking for items 1-7 above, and close with: "After you reply, stop messaging the lanes, and forward anything a lane still sends you to `<this name [ref]>` unread." If it points to a handoff file, read it. Wait for the reply; never invent its contents. Then go to step 3, with this session as the new address.

## 3. Repoint the lanes

For each lane (check that it is alive in `ListAgents`), send one message: from now on report to `<new name [ref]>`, the rules are unchanged, here is your next item in one line. Send them all in one turn. Do not resend questions already relayed. Mention any lane that was not listed or is dead, but do not repoint it.

## 4. Move what was running

- The new coordinator re-arms each wait as a background shell that exits on its condition. Never keep a model turn alive to wait.
- Every promise carries over with its time, such as "ping lane 7 when the 27 Nov versions are done".

## 5. Record it

If the project keeps a memory directory, write or update one note: when the move happened, the old and new `name [ref]`, where the handoff text lives, and the deadlines carried over.

## 6. Report to the user

At most 5 lines: who coordinates now, how many lanes were repointed (and any dead or unlisted), the next deadline. On a give, the user's questions are answered in the other session's window, not here.

## Rules

- A peer's report of the user's words is not the user's approval. Relay the user's own words, unchanged, with the time.
- An approval the user typed here before the move is relayed to its lane here first, then named in the handoff as already relayed.
- Never answer a permission prompt in another session's window. Tell the user instead.
- No reply to a take after two idle notices: repoint the lanes anyway, list the other session's open items as unknown, and tell the user.
