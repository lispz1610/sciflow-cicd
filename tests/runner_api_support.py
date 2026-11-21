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
