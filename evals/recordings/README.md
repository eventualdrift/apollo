# Replay recordings

`brain.fake` in `replay` mode reads this directory (spec G.4). A recording is filed under the
`bundle_hash` it answers and matched on the `rendered_prompt_hash` it was produced from, so a
`render_version` change retires a recording rather than answering for bytes that were never sent.

The format is written and validated by `apollo.evals.recordings`. A recording holds the visible
generation only — text, finish reason, model identifier, token counts — and never hidden reasoning,
provider secrets, response headers or a raw HTTP body.

The directory is empty at this milestone. Recordings are captured from real runs; none has been made
here, and nothing fabricates one to fill the gap.
