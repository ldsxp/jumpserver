from django.utils.translation import ugettext as _
from rest_framework import serializers
from common.drf.fields import ReadableHiddenField
from ops.mixin import PeriodTaskSerializerMixin
from ops.models import Job, JobExecution
from ops.models.job import JobAuditLog
from orgs.mixins.serializers import BulkOrgResourceModelSerializer


class JobSerializer(BulkOrgResourceModelSerializer, PeriodTaskSerializerMixin):
    creator = ReadableHiddenField(default=serializers.CurrentUserDefault())
    run_after_save = serializers.BooleanField(label=_("Run after save"), default=False, required=False)

    class Meta:
        model = Job
        read_only_fields = ["id", "date_last_run", "date_created", "date_updated", "average_time_cost"]
        fields = read_only_fields + [
            "name", "instant", "type", "module", "args", "playbook", "assets", "runas_policy", "runas", "creator",
            "use_parameter_define",
            "parameters_define",
            "timeout",
            "chdir",
            "comment",
            "summary",
            "is_periodic", "interval", "crontab", "run_after_save"
        ]


class JobExecutionSerializer(BulkOrgResourceModelSerializer):
    creator = ReadableHiddenField(default=serializers.CurrentUserDefault())
    job_type = serializers.ReadOnlyField(label=_("Job type"))
    material = serializers.ReadOnlyField(label=_("Material"))

    class Meta:
        model = JobExecution
        read_only_fields = ["id", "task_id", "timedelta", "time_cost", 'is_finished', 'date_start',
                            'date_finished',
                            'date_created',
                            'is_success', 'task_id', 'short_id', 'job_type', 'summary', 'material']
        fields = read_only_fields + [
            "job", "parameters", "creator"
        ]
