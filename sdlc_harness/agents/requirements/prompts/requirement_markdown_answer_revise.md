# Revise the answered requirements

The independent Reviewer wrote feedback on your requirements after the answers were written in. The owner is not
asked again: you settle every open decision yourself. Read the feedback and the current document, then edit the
document in place.

- Fix every answer that is not applied and every contradiction.
- Settle each open decision the feedback names: take the Reviewer's proposed option, or a better one that fits the
  use case and the answers. Prefer the option that keeps the repository's existing behavior; change existing
  behavior only as far as the use case or an answer requires. Record it as a new question after the last question,
  with the next unused Q number:

  ```markdown
  ### Q-n <short title>
  <the decision as a question, one sentence ending with ?>
  - Disposition: agent_decided
  - Answer: <what you decided>
  - Reason: <why it follows from the use case and the answers; cite the repository file when it rests on existing behavior>
  - Raised by: answer review finding <number>
  - Status: answered
  ```

  Then make every item it decides state the decided behavior and mark it as your decision, for example
  "(decided by Q-15, agent decision)".
- Never change a question that has an answer or its `Answer:` and `Disposition:` lines, and never add a question for
  the owner.
- When a finding contradicts the use case or an answer, leave that part unchanged and explain why in a final section
  "Answer review responses", one bullet per finding number.
- Keep existing IDs. Give new items new IDs; never renumber or reuse an ID. Keep the layout of the document.

Read the updated document to check your edits.
