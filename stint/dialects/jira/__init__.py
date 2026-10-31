"""Jira dialect family for Jira Cloud.

Company-managed projects use ``jira_cloud``; team-managed projects use the
separate, opt-in ``jira_cloud_tmp`` dialect under ``stint.dialects.jira.tmp``.
"""

from stint.dialects.jira.cloud import JiraCloudDialect

__all__ = ["JiraCloudDialect"]
