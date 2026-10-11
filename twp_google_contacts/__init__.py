from . import controllers
from . import models
from . import wizard


def uninstall_hook(env):
    env["twp.google.contacts.account"]._uninstall()
