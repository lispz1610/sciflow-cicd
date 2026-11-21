"""Shared runner-inventory mock behavior for API test doubles."""

from __future__ import annotations


class RunnerInventoryApiMixin:
    runner_inventory_path = "/repos/acme/project/actions/runners"

    def init_runner_inventory(self, total_count=0, status=200):
        self.runner_status = status
        self.runner_total_count = total_count
        self.runner_reads = []

    def runner_inventory_response(self, path):
        if path != self.runner_inventory_path:
            return None
        self.runner_reads.append(path)
        if self.runner_status != 200:
            return self.runner_status, {"message": "runner inventory unavailable"}
        return 200, {"total_count": self.runner_total_count if self.runner_total_count is not None else 0}


class RunnerInventoryAwareApiMixin(RunnerInventoryApiMixin):
    def __init__(self, total_count=0, status=200, **kwargs):
        super().__init__(**kwargs)
        self.init_runner_inventory(total_count=total_count, status=status)
        self.runner_inventory_path = self.base + "/actions/runners"

    def request(self, method, path, payload=None):
        if method == "GET":
            runner_response = self.runner_inventory_response(path)
            if runner_response is not None:
                return runner_response
        return super().request(method, path, payload)
