"""Bound one paid attempt to a durable, owner-scoped reservation."""
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5


def guarded_request(reserve, call, *, owner_id, project_id, agent, ceiling_cents, model):
    def request(system, prompt):
        if ceiling_cents <= 0:
            raise RuntimeError("Не задана предельная стоимость запроса.")
        payload = f"{model}\0{system}\0{prompt}".encode("utf-8")
        if len(payload) > 128_000:
            raise RuntimeError("Контекст запроса превышает размер, предусмотренный бюджетом.")
        operation_id = uuid5(NAMESPACE_URL, sha256(payload).hexdigest())
        decision = reserve({
            "p_owner": owner_id, "p_project": project_id, "p_agent": agent,
            "p_operation": str(operation_id), "p_estimate": ceiling_cents,
        })
        if decision == "already_reserved":
            raise RuntimeError("Этот запрос уже зарезервирован. Сначала проверьте журнал расходов.")
        if decision != "reserved":
            raise RuntimeError("Платная операция остановлена: бюджет отсутствует или исчерпан.")
        return call(system, prompt)
    return request
