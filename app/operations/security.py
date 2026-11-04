"""
Encryption — Phase 5.

Uses pikepdf's R=6 (AES-256) encryption support. Validates boolean
permission flags strictly so strings like "false" are never coerced to True.
"""
import os
import tempfile
import pikepdf
from app import storage
from app.operations import parse_bool


def encrypt(file_id, user_password: str = "", owner_password: str = None,
            allow_printing: bool = True, allow_copying: bool = True,
            allow_modification: bool = False):
    if user_password is None:
        user_password = ""
    if not isinstance(user_password, str):
        raise ValueError("user_password must be a string")
    if owner_password is not None and not isinstance(owner_password, str):
        raise ValueError("owner_password must be a string")
    if not user_password and not owner_password:
        raise ValueError("provide at least a user_password or owner_password")

    allow_printing = parse_bool(allow_printing, "allow_printing")
    allow_copying = parse_bool(allow_copying, "allow_copying")
    allow_modification = parse_bool(allow_modification, "allow_modification")
    owner_password = owner_password or user_password or os.urandom(16).hex()

    src = storage.resolve(file_id)
    permissions = pikepdf.Permissions(
        extract=allow_copying,
        print_lowres=allow_printing,
        print_highres=allow_printing,
        modify_annotation=allow_modification,
        modify_assembly=allow_modification,
        modify_form=allow_modification,
        modify_other=allow_modification,
    )

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        with pikepdf.open(src) as pdf:
            pdf.save(
                out_path,
                encryption=pikepdf.Encryption(
                    user=user_password, owner=owner_password, R=6,
                    allow=permissions,
                ),
            )
    except Exception:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise
    return storage.save_output_path(out_path, ".pdf")


def decrypt(file_id, password: str = ""):
    """Removes encryption entirely."""
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    src = storage.resolve(file_id)
    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        with pikepdf.open(src, password=password) as pdf:
            pdf.save(out_path)
    except pikepdf.PasswordError as e:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise ValueError("incorrect password") from e
    except Exception:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise
    return storage.save_output_path(out_path, ".pdf")


def inspect(file_id, password: str = ""):
    """Returns encryption status without needing write access."""
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    src = storage.resolve(file_id)
    try:
        with pikepdf.open(src, password=password) as pdf:
            return {
                "encrypted": pdf.is_encrypted,
                "requires_password": False,
                "permissions": {
                    "extract": pdf.allow.extract,
                    "print_lowres": pdf.allow.print_lowres,
                    "print_highres": pdf.allow.print_highres,
                    "modify_annotation": pdf.allow.modify_annotation,
                    "modify_assembly": pdf.allow.modify_assembly,
                    "modify_form": pdf.allow.modify_form,
                    "modify_other": pdf.allow.modify_other,
                } if pdf.is_encrypted else None,
            }
    except pikepdf.PasswordError:
        return {"encrypted": True, "requires_password": True, "permissions": None}
