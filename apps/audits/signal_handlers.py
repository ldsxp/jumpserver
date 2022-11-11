# -*- coding: utf-8 -*-
#
from django.conf import settings
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone, translation
from django.utils.functional import LazyObject
from django.contrib.auth import BACKEND_SESSION_KEY
from django.utils.translation import ugettext_lazy as _
from django.db.models.signals import post_save, m2m_changed, pre_delete
from rest_framework.request import Request
from rest_framework.renderers import JSONRenderer

from orgs.utils import current_org
from perms.models import AssetPermission
from users.models import User
from users.signals import post_user_change_password
from assets.models import Asset
from jumpserver.utils import current_request
from authentication.signals import post_auth_failed, post_auth_success
from authentication.utils import check_different_city_login_if_need
from terminal.models import Session, Command
from terminal.serializers import SessionSerializer
from terminal.backends.command.serializers import SessionCommandSerializer
from common.const.signals import POST_ADD, POST_REMOVE, POST_CLEAR
from common.utils import get_request_ip, get_logger, get_syslogger
from common.utils.encode import data_to_json
from . import models, serializers
from .const import ActionChoices
from .utils import write_login_log, create_operate_log


logger = get_logger(__name__)
sys_logger = get_syslogger(__name__)
json_render = JSONRenderer()


class AuthBackendLabelMapping(LazyObject):
    @staticmethod
    def get_login_backends():
        backend_label_mapping = {}
        for source, backends in User.SOURCE_BACKEND_MAPPING.items():
            for backend in backends:
                backend_label_mapping[backend] = source.label
        backend_label_mapping[settings.AUTH_BACKEND_PUBKEY] = _("SSH Key")
        backend_label_mapping[settings.AUTH_BACKEND_MODEL] = _("Password")
        backend_label_mapping[settings.AUTH_BACKEND_SSO] = _("SSO")
        backend_label_mapping[settings.AUTH_BACKEND_AUTH_TOKEN] = _("Auth Token")
        backend_label_mapping[settings.AUTH_BACKEND_WECOM] = _("WeCom")
        backend_label_mapping[settings.AUTH_BACKEND_FEISHU] = _("FeiShu")
        backend_label_mapping[settings.AUTH_BACKEND_DINGTALK] = _("DingTalk")
        backend_label_mapping[settings.AUTH_BACKEND_TEMP_TOKEN] = _("Temporary token")
        return backend_label_mapping

    def _setup(self):
        self._wrapped = self.get_login_backends()


AUTH_BACKEND_LABEL_MAPPING = AuthBackendLabelMapping()


M2M_NEED_RECORD = {
    User.groups.through.__name__: (
        _("User and Group"),
        _("{User} JOINED {UserGroup}"),
        _("{User} LEFT {UserGroup}"),
    ),
    Asset.nodes.through.__name__: (
        _("Node and Asset"),
        _("{Node} ADD {Asset}"),
        _("{Node} REMOVE {Asset}"),
    ),
    AssetPermission.users.through.__name__: (
        _("User asset permissions"),
        _("{AssetPermission} ADD {User}"),
        _("{AssetPermission} REMOVE {User}"),
    ),
    AssetPermission.user_groups.through.__name__: (
        _("User group asset permissions"),
        _("{AssetPermission} ADD {UserGroup}"),
        _("{AssetPermission} REMOVE {UserGroup}"),
    ),
    AssetPermission.assets.through.__name__: (
        _("Asset permission"),
        _("{AssetPermission} ADD {Asset}"),
        _("{AssetPermission} REMOVE {Asset}"),
    ),
    AssetPermission.nodes.through.__name__: (
        _("Node permission"),
        _("{AssetPermission} ADD {Node}"),
        _("{AssetPermission} REMOVE {Node}"),
    ),
}

M2M_ACTION_MAPER = {
    POST_ADD: ActionChoices.create,
    POST_REMOVE: ActionChoices.delete,
    POST_CLEAR: ActionChoices.delete,
}


@receiver(m2m_changed)
def on_m2m_changed(sender, action, instance, model, pk_set, **kwargs):
    if action not in M2M_ACTION_MAPER:
        return

    user = current_request.user if current_request else None
    if not user or not user.is_authenticated:
        return

    sender_name = sender.__name__
    if sender_name in M2M_NEED_RECORD:
        org_id = current_org.id
        remote_addr = get_request_ip(current_request)
        user = str(user)
        resource_type, resource_tmpl_add, resource_tmpl_remove = M2M_NEED_RECORD[
            sender_name
        ]

        action = M2M_ACTION_MAPER[action]
        if action == ActionChoices.create:
            resource_tmpl = resource_tmpl_add
        elif action == ActionChoices.delete:
            resource_tmpl = resource_tmpl_remove
        else:
            return

        to_create = []
        objs = model.objects.filter(pk__in=pk_set)

        if isinstance(instance, Asset):
            instance_name = Asset.__name__
        else:
            instance_name = instance.__class__.__name__
        instance_value = str(instance)
        model_name = model.__name__

        print("Instace name: ", instance_name, instance_value)
        for obj in objs:
            resource = resource_tmpl.format(
                **{instance_name: instance_value, model_name: str(obj)}
            )[
                :128
            ]  # `resource` 字段只有 128 个字符长 😔

            to_create.append(
                models.OperateLog(
                    user=user,
                    action=action,
                    resource_type=resource_type,
                    resource=resource,
                    remote_addr=remote_addr,
                    org_id=org_id,
                )
            )
        models.OperateLog.objects.bulk_create(to_create)


@receiver(post_save)
def on_object_created_or_update(
    sender, instance=None, created=False, update_fields=None, **kwargs
):
    # last_login 改变是最后登录日期, 每次登录都会改变
    if (
        instance._meta.object_name == "User"
        and update_fields
        and "last_login" in update_fields
    ):
        return
    if created:
        action = ActionChoices.create
    else:
        action = ActionChoices.update
    create_operate_log(action, sender, instance)


@receiver(pre_delete)
def on_object_delete(sender, instance=None, **kwargs):
    create_operate_log(ActionChoices.delete, sender, instance)


@receiver(post_user_change_password, sender=User)
def on_user_change_password(sender, user=None, **kwargs):
    if not current_request:
        remote_addr = "127.0.0.1"
        change_by = "System"
    else:
        remote_addr = get_request_ip(current_request)
        if not current_request.user.is_authenticated:
            change_by = str(user)
        else:
            change_by = str(current_request.user)
    with transaction.atomic():
        models.PasswordChangeLog.objects.create(
            user=str(user),
            change_by=change_by,
            remote_addr=remote_addr,
        )


def on_audits_log_create(sender, instance=None, **kwargs):
    if sender == models.UserLoginLog:
        category = "login_log"
        serializer_cls = serializers.UserLoginLogSerializer
    elif sender == models.FTPLog:
        category = "ftp_log"
        serializer_cls = serializers.FTPLogSerializer
    elif sender == models.OperateLog:
        category = "operation_log"
        serializer_cls = serializers.OperateLogSerializer
    elif sender == models.PasswordChangeLog:
        category = "password_change_log"
        serializer_cls = serializers.PasswordChangeLogSerializer
    elif sender == Session:
        category = "host_session_log"
        serializer_cls = SessionSerializer
    elif sender == Command:
        category = "session_command_log"
        serializer_cls = SessionCommandSerializer
    else:
        return

    serializer = serializer_cls(instance)
    data = data_to_json(serializer.data, indent=None)
    msg = "{} - {}".format(category, data)
    sys_logger.info(msg)


def get_login_backend(request):
    backend = request.session.get("auth_backend", "") or request.session.get(
        BACKEND_SESSION_KEY, ""
    )

    backend_label = AUTH_BACKEND_LABEL_MAPPING.get(backend, None)
    if backend_label is None:
        backend_label = ""
    return backend_label


def generate_data(username, request, login_type=None):
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    login_ip = get_request_ip(request) or "0.0.0.0"

    if login_type is None and isinstance(request, Request):
        login_type = request.META.get("HTTP_X_JMS_LOGIN_TYPE", "U")
    if login_type is None:
        login_type = "W"

    with translation.override("en"):
        backend = str(get_login_backend(request))

    data = {
        "username": username,
        "ip": login_ip,
        "type": login_type,
        "user_agent": user_agent[0:254],
        "datetime": timezone.now(),
        "backend": backend,
    }
    return data


@receiver(post_auth_success)
def on_user_auth_success(sender, user, request, login_type=None, **kwargs):
    logger.debug("User login success: {}".format(user.username))
    check_different_city_login_if_need(user, request)
    data = generate_data(user.username, request, login_type=login_type)
    request.session["login_time"] = data["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    data.update({"mfa": int(user.mfa_enabled), "status": True})
    write_login_log(**data)


@receiver(post_auth_failed)
def on_user_auth_failed(sender, username, request, reason="", **kwargs):
    logger.debug("User login failed: {}".format(username))
    data = generate_data(username, request)
    data.update({"reason": reason[:128], "status": False})
    write_login_log(**data)
