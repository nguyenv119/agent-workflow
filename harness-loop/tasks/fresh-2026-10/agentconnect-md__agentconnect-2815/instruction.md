The system design for channel session modes states that an `append` session forbids changing visibility (`setVisibility`). However, the authorization policy does not currently enforce this. 

Currently, `AuthorizationAction.SessionChangeVisibility` in the control plane's authorization policy (`policy.ts`) checks only that the identity owns the session. Because the owner on an org channel session is just the first poster, that user could change the whole channel's long session to `private`. This hides every later message from the rest of the channel and stops shared-memory capture for the conversation.

Expected behavior:
1. Refuse `session.visibility.change` (`SessionChangeVisibility`) in the control plane authorization policy for any session whose `thread` coordinate is an `append` coordinate, regardless of whether the user owns the session.
2. The `SessionViewable` interface should gain an optional `thread?: string | null` property. If the `thread` field is omitted on the resource passed to the authorization check, the policy must fail closed and treat the coordinate as unread/an `append` session (refusing the action).
3. In the database repository layer (e.g., `session.repo.ts`), the `setVisibility` locked-row recheck (the `FOR UPDATE` read) must now select the `thread` field so that the authorization policy predicate correctly evaluates the thread inside the write transaction.
4. Create a new module at `/app/src/packages/protocol/src/append-coordinate.ts` exporting an `APPEND_COORDINATE_PREFIX` constant (`'append:'`) and an `isAppendCoordinate(thread: string | undefined | null) -> boolean` utility function. Re-export this new module from the protocol package's main index, and use this helper in the control-plane policy and daemon to evaluate `append:` coordinates.

By fixing the authorization policy, the console's session detail DTO will correctly report `canChangeVisibility: false` for append sessions, effectively hiding the visibility control from the UI.