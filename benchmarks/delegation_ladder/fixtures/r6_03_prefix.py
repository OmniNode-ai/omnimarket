class HandlerProjectionDelegation:
    def project_judge_verdict(
        self, verdict: ModelDelegationJudgeVerdictEvent, db: DatabaseAdapter
    ) -> dict[str, object]:
        return {"projected": verdict.delegation_id, "verdict": verdict.verdict}

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim."""
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event_type = str(payload.pop("_event_type", ""))
        if "delegation-judge-verdict" in event_type:
            verdict = ModelDelegationJudgeVerdictEvent(**payload)
            return self.project_judge_verdict(verdict, db_raw)
        raise ValueError(f"unroutable event type: {event_type}")
