# Step Functions Redrive Notes

Redrive is not a full rerun.

- A redrive resumes from the failed or aborted state and reuses outputs from states that already succeeded.
- If you need every stage to run against newly published ECS images or Lambda code, start a new execution instead of redriving the old one.
- In this system, failed ECS stages can pick up newly registered task definition revisions because the state machine references ECS task families, not pinned revisions.
- Failed Lambda stages can pick up newly deployed images because the state machine invokes unqualified function ARNs.
- A redrive still uses the original execution's state machine definition, so state machine definition changes are not picked up by redriving an older execution.
