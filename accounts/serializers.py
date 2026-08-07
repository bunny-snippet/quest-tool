from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from .access import assignable_functions, assignable_roles, effective_permission_codes
from .models import AccessFunction, EmployeeProfile, Role, RoleFunctionPermission, UserFunctionOverride


class AccessFunctionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccessFunction
        fields = ["id", "code", "name", "module", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class RoleSerializer(serializers.ModelSerializer):
    permission_codes = serializers.ListField(child=serializers.CharField(max_length=120), required=False, write_only=True)
    effective_permission_codes = serializers.SerializerMethodField()
    employee_count = serializers.IntegerField(read_only=True, default=0)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = Role
        fields = [
            "id", "name", "slug", "description", "rank", "is_system", "is_active", "employee_count",
            "permission_codes", "effective_permission_codes", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["is_system", "created_at", "updated_at"]

    def get_effective_permission_codes(self, obj) -> list[str]:
        return list(obj.function_assignments.filter(allowed=True, function__is_active=True).values_list("function__code", flat=True))

    def validate_permission_codes(self, codes):
        codes = list(dict.fromkeys(codes))
        found = set(AccessFunction.objects.filter(code__in=codes).values_list("code", flat=True))
        missing = sorted(set(codes) - found)
        if missing:
            raise serializers.ValidationError(f"Unknown function codes: {', '.join(missing)}")
        request = self.context.get("request")
        if request and not request.user.is_superuser:
            grantable = set(assignable_functions(request.user).values_list("code", flat=True))
            forbidden = sorted(set(codes) - grantable)
            if forbidden:
                raise serializers.ValidationError(f"You cannot delegate functions you do not have: {', '.join(forbidden)}")
        return codes

    def _set_permissions(self, role, codes):
        functions = AccessFunction.objects.filter(code__in=codes)
        role.function_assignments.exclude(function__in=functions).delete()
        for function in functions:
            RoleFunctionPermission.objects.update_or_create(role=role, function=function, defaults={"allowed": True})

    @transaction.atomic
    def create(self, validated_data):
        codes = validated_data.pop("permission_codes", [])
        request = self.context.get("request")
        if request:
            validated_data["created_by"] = request.user
        role = super().create(validated_data)
        self._set_permissions(role, codes)
        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        codes = validated_data.pop("permission_codes", None)
        role = super().update(instance, validated_data)
        if codes is not None:
            self._set_permissions(role, codes)
        return role


class UserAccessSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    role = serializers.SlugField(write_only=True, required=False, allow_null=True)
    role_details = serializers.SerializerMethodField()
    allow_codes = serializers.ListField(child=serializers.CharField(max_length=120), required=False, write_only=True)
    deny_codes = serializers.ListField(child=serializers.CharField(max_length=120), required=False, write_only=True)
    allowed_overrides = serializers.SerializerMethodField()
    denied_overrides = serializers.SerializerMethodField()
    effective_permissions = serializers.SerializerMethodField()
    password = serializers.CharField(write_only=True, required=False, min_length=8)
    account_type = serializers.ChoiceField(choices=EmployeeProfile.AccountType.choices, write_only=True, required=False)
    account_type_details = serializers.SerializerMethodField()
    company_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=160)
    company = serializers.CharField(source="employee_profile.company_name", read_only=True)
    department = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=120)
    sub_branch = serializers.CharField(source="employee_profile.department", read_only=True)
    created_by = serializers.CharField(source="employee_profile.created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = get_user_model()
        fields = [
            "id", "username", "first_name", "last_name", "full_name", "email", "is_active", "last_login",
            "role", "role_details", "allow_codes", "deny_codes", "allowed_overrides", "denied_overrides",
            "effective_permissions", "password", "account_type", "account_type_details", "company_name", "company",
            "department", "sub_branch", "created_by",
        ]
        extra_kwargs = {"username": {"required": False}}
        read_only_fields = ["last_login"]

    def get_full_name(self, obj) -> str:
        return obj.get_full_name() or obj.username

    def get_role_details(self, obj) -> dict | None:
        profile = getattr(obj, "employee_profile", None)
        return {"slug": profile.role.slug, "name": profile.role.name} if profile and profile.role else None

    def get_account_type_details(self, obj) -> dict:
        profile = getattr(obj, "employee_profile", None)
        return {"value": profile.account_type, "label": profile.get_account_type_display()} if profile else {"value": "employee", "label": "Employee"}

    def get_allowed_overrides(self, obj) -> list[str]:
        return list(obj.function_overrides.filter(effect=UserFunctionOverride.Effect.ALLOW).values_list("function__code", flat=True))

    def get_denied_overrides(self, obj) -> list[str]:
        return list(obj.function_overrides.filter(effect=UserFunctionOverride.Effect.DENY).values_list("function__code", flat=True))

    def get_effective_permissions(self, obj) -> list[str]:
        return sorted(effective_permission_codes(obj))

    def validate_role(self, slug):
        request = self.context.get("request")
        queryset = assignable_roles(request.user) if request else Role.objects.filter(is_active=True)
        if slug is not None and not queryset.filter(slug=slug).exists():
            raise serializers.ValidationError("Unknown or inactive role.")
        return slug

    def validate_email(self, value):
        queryset = get_user_model().objects.filter(email__iexact=value) | get_user_model().objects.filter(username__iexact=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower()

    def _validate_codes(self, codes):
        found = set(AccessFunction.objects.filter(code__in=codes).values_list("code", flat=True))
        missing = sorted(set(codes) - found)
        if missing:
            raise serializers.ValidationError(f"Unknown function codes: {', '.join(missing)}")
        request = self.context.get("request")
        if request and not request.user.is_superuser:
            grantable = set(assignable_functions(request.user).values_list("code", flat=True))
            forbidden = sorted(set(codes) - grantable)
            if forbidden:
                raise serializers.ValidationError(f"You cannot delegate functions you do not have: {', '.join(forbidden)}")
        return list(dict.fromkeys(codes))

    def validate_allow_codes(self, codes):
        return self._validate_codes(codes)

    def validate_deny_codes(self, codes):
        return self._validate_codes(codes)

    def _update_access(
        self, user, role_slug, allow_codes, deny_codes, account_type=serializers.empty,
        company_name=serializers.empty, department=serializers.empty,
    ):
        profile, _ = EmployeeProfile.objects.get_or_create(user=user)
        if role_slug is not serializers.empty:
            profile.role = Role.objects.filter(slug=role_slug).first() if role_slug else None
        if account_type is not serializers.empty:
            profile.account_type = account_type
        if company_name is not serializers.empty:
            profile.company_name = company_name
        if department is not serializers.empty:
            profile.department = department
        profile.save()
        user.employee_profile = profile
        if allow_codes is serializers.empty and deny_codes is serializers.empty:
            return
        allow_codes = [] if allow_codes is serializers.empty else allow_codes
        deny_codes = [] if deny_codes is serializers.empty else deny_codes
        selected = set(allow_codes) | set(deny_codes)
        user.function_overrides.exclude(function__code__in=selected).delete()
        effects = [(allow_codes, UserFunctionOverride.Effect.ALLOW), (deny_codes, UserFunctionOverride.Effect.DENY)]
        for codes, effect in effects:
            for function in AccessFunction.objects.filter(code__in=codes):
                UserFunctionOverride.objects.update_or_create(user=user, function=function, defaults={"effect": effect})

    @transaction.atomic
    def create(self, validated_data):
        role_slug = validated_data.pop("role", "employee")
        allow_codes = validated_data.pop("allow_codes", [])
        deny_codes = validated_data.pop("deny_codes", [])
        password = validated_data.pop("password", None)
        account_type = validated_data.pop("account_type", EmployeeProfile.AccountType.EMPLOYEE)
        company_name = validated_data.pop("company_name", "")
        department = validated_data.pop("department", "")
        if not password:
            raise serializers.ValidationError({"password": "Password is required when creating a user."})
        if not validated_data.get("email"):
            raise serializers.ValidationError({"email": "Email is required."})
        validated_data.setdefault("username", validated_data["email"].lower())
        request = self.context.get("request")
        if request and not assignable_roles(request.user).filter(slug=role_slug).exists():
            raise serializers.ValidationError({"role": "You cannot assign this role."})
        user = get_user_model()(**validated_data)
        user.set_password(password) if password else user.set_unusable_password()
        user.save()
        profile, _ = EmployeeProfile.objects.get_or_create(user=user)
        profile.created_by = request.user if request else None
        profile.save(update_fields=["created_by", "updated_at"])
        self._update_access(user, role_slug, allow_codes, deny_codes, account_type, company_name, department)
        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        role_slug = validated_data.pop("role", serializers.empty)
        allow_codes = validated_data.pop("allow_codes", serializers.empty)
        deny_codes = validated_data.pop("deny_codes", serializers.empty)
        password = validated_data.pop("password", None)
        account_type = validated_data.pop("account_type", serializers.empty)
        company_name = validated_data.pop("company_name", serializers.empty)
        department = validated_data.pop("department", serializers.empty)
        previous_email = instance.email
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if "email" in validated_data and (instance.username == previous_email or "@" in instance.username):
            instance.username = validated_data["email"]
        if password:
            instance.set_password(password)
        instance.save()
        self._update_access(instance, role_slug, allow_codes, deny_codes, account_type, company_name, department)
        return instance
