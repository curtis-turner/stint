"""Jira dialect family. Cloud-only as of 0.1; DC was dropped (see plan_pensum.md)."""

from pensum.dialects.jira.cloud import JiraCloudDialect

__all__ = ["JiraCloudDialect"]
