"""External API endpoints"""

from datetime import timedelta
from logging import getLogger

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from lasuite.oidc_resource_server.authentication import ResourceServerAuthentication
from rest_framework import decorators, mixins, viewsets
from rest_framework import (
    exceptions as drf_exceptions,
)
from rest_framework import (
    parsers as drf_parsers,
)
from rest_framework import (
    response as drf_response,
)
from rest_framework import (
    status as drf_status,
)

from core import analytics, api, models, utils
from core.api.feature_flag import FeatureFlag
from core.services.jwt_token import JwtTokenService

from ..services.provisional_user_service import (
    ProvisionalUserCreationDisabledError,
    ProvisionalUserIntegrityError,
    ProvisionalUserService,
)
from . import authentication, permissions, serializers

logger = getLogger(__name__)


class ApplicationViewSet(viewsets.ViewSet):
    """API endpoints for application authentication and token generation."""

    @decorators.action(
        detail=False,
        methods=["post"],
        url_path="token",
        url_name="token",
        parser_classes=[drf_parsers.FormParser, drf_parsers.JSONParser],
    )
    @FeatureFlag.require("application")
    def generate_jwt_access_token(self, request, *args, **kwargs):
        """Generate JWT access token for application delegation.

        Validates application credentials and generates a JWT token scoped
        to a specific user email, allowing the application to act on behalf
        of that user.

        Note: The 'scope' parameter accepts an email address to identify the user
        being delegated. This design allows applications to obtain user-scoped tokens
        for delegation purposes. The scope field is intentionally generic and can be
        extended to support other values in the future.

        Reference: https://stackoverflow.com/a/27711422
        """
        serializer = serializers.ApplicationJwtSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        client_id = serializer.validated_data["client_id"]
        client_secret = serializer.validated_data["client_secret"]

        try:
            application = models.Application.objects.get(client_id=client_id)
        except models.Application.DoesNotExist as e:
            raise drf_exceptions.AuthenticationFailed("Invalid credentials") from e

        if not check_password(client_secret, application.client_secret):
            raise drf_exceptions.AuthenticationFailed("Invalid credentials")

        if not application.is_active:
            raise drf_exceptions.AuthenticationFailed("Application is inactive")

        email = serializer.validated_data["scope"]
        try:
            validate_email(email)
        except ValidationError:
            return drf_response.Response(
                {
                    "error": "Scope should be a valid email address.",
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        if not application.can_delegate_email(email):
            logger.warning(
                "Application %s denied delegation for %s",
                application.client_id,
                email,
            )
            return drf_response.Response(
                {
                    "error": "This application is not authorized for this email domain.",
                },
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        try:
            user, _ = ProvisionalUserService().get_or_create(email, client_id)
        except ProvisionalUserCreationDisabledError as not_found_error:
            raise drf_exceptions.NotFound("User not found.") from not_found_error
        except ProvisionalUserIntegrityError:
            return drf_response.Response(
                {"error": "Failed to create or retrieve provisional user."},
                status=drf_status.HTTP_409_CONFLICT,
            )

        scope = " ".join(application.scopes or [])

        token_service = JwtTokenService(
            secret_key=settings.APPLICATION_JWT_SECRET_KEY,
            algorithm=settings.APPLICATION_JWT_ALG,
            issuer=settings.APPLICATION_JWT_ISSUER,
            audience=settings.APPLICATION_JWT_AUDIENCE,
            expiration_seconds=settings.APPLICATION_JWT_EXPIRATION_SECONDS,
            token_type=settings.APPLICATION_JWT_TOKEN_TYPE,
        )

        data = token_service.generate_jwt(
            user,
            scope,
            {
                "client_id": client_id,
                "delegated": True,
            },
        )

        return drf_response.Response(
            data,
            status=drf_status.HTTP_200_OK,
        )


class RoomViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """Application-delegated API for room management.

    Provides JWT-authenticated access to room operations for external applications
    acting on behalf of users. All operations are scope-based and filtered to the
    authenticated user's accessible rooms.

    Supported operations:
    - list: List rooms the user has access to (requires 'rooms:list' scope)
    - retrieve: Get room details (requires 'rooms:retrieve' scope)
    - create: Create a new room owned by the user (requires 'rooms:create' scope)
    - livekit-token: Mint a LiveKit token for the user in a room (applications
      only, requires 'rooms:livekit_token' scope; no role on the room needed,
      the application vouches for the user)
    """

    authentication_classes = [
        authentication.ApplicationJWTAuthentication,
        authentication.AddonsJWTAuthentication,
        ResourceServerAuthentication,
    ]
    permission_classes = [
        api.permissions.IsAuthenticated
        & permissions.HasRequiredRoomScope
        & permissions.RoomPermissions
    ]
    queryset = models.Room.objects.all()
    serializer_class = serializers.RoomSerializer

    def list(self, request, *args, **kwargs):
        """Limit listed rooms to the ones related to the authenticated user."""

        user = self.request.user

        if user.is_authenticated:
            queryset = (
                self.filter_queryset(self.get_queryset()).filter(users=user).distinct()
            )
        else:
            queryset = self.get_queryset().none()

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return drf_response.Response(serializer.data)

    @decorators.action(
        detail=True,
        methods=["post"],
        url_path="livekit-token",
        url_name="livekit-token",
        # Applications only: the token is minted for any room, with the role
        # the caller gives, so no token an end user can hold may reach it
        authentication_classes=[authentication.ApplicationJWTAuthentication],
        permission_classes=[
            api.permissions.IsAuthenticated & permissions.HasRequiredRoomScope
        ],
    )
    def livekit_token(self, request, pk=None):  # pylint: disable=unused-argument
        """Mint a LiveKit token for the delegated user, with the identity the
        application imposes (`@user:server:device`, as a MatrixRTC client
        derives it). Meet then knows the participant: recording, transcription
        and the raised hands follow its rules."""
        serializer = serializers.LivekitTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        room = self.get_object()
        user = request.user
        client_id = (request.auth or {}).get("client_id")

        # Only the rooms this application created: its secret, once leaked,
        # must not open the rooms people made in Meet
        application = room.created_by_application
        if application is None or application.client_id != client_id:
            raise drf_exceptions.NotFound()

        sub = data.get("sub")
        if sub and not user.sub:
            user.sub = sub
            try:
                user.save(update_fields=["sub"])
            except ValidationError:
                # Say nothing of it to the caller: the answer would tell
                # whether a sub exists
                user.sub = None
                logger.warning(
                    "Sub refused via application: user_id=%s, client_id=%s",
                    user.id,
                    client_id,
                )
            else:
                logger.info(
                    "Sub set via application: user_id=%s, client_id=%s",
                    user.id,
                    client_id,
                )

        identity = data["identity"]
        matrix_user_id, _, matrix_device_id = identity.rpartition(":")
        role = data.get("role")
        livekit_config = utils.generate_livekit_config(
            room_id=str(room.id),
            user=user,
            username=data.get("username"),
            role=role,
            configuration=room.configuration,
            identity=identity,
            extra_attributes={
                "matrix_user_id": matrix_user_id,
                "matrix_device_id": matrix_device_id,
            },
            ttl=timedelta(seconds=settings.APPLICATION_LIVEKIT_TOKEN_TTL_SECONDS),
        )

        # Log for auditing
        logger.info(
            "LiveKit token minted via application: "
            "room_id=%s, user_id=%s, identity=%s, role=%s, client_id=%s",
            room.id,
            user.id,
            identity,
            role,
            client_id,
        )

        return drf_response.Response(livekit_config)

    def perform_create(self, serializer):
        """Set the current user as owner of the newly created room, and keep
        the application that created it."""
        application = None
        if isinstance(
            self.request.successful_authenticator,
            authentication.ApplicationJWTAuthentication,
        ):
            application = models.Application.objects.filter(
                client_id=self.request.auth.get("client_id")
            ).first()
        room = serializer.save(created_by_application=application)
        models.ResourceAccess.objects.create(
            resource=room,
            user=self.request.user,
            role=models.RoleChoices.OWNER,
        )

        auth_method = type(self.request.successful_authenticator).__name__
        client_id = (self.request.auth or {}).get("client_id", "unknown")

        # Log for auditing
        logger.info(
            "Room created via application: room_id=%s, user_id=%s, client_id=%s, auth_method=%s",
            room.id,
            self.request.user.id,
            client_id,
            auth_method,
        )

        analytics.capture(
            self.request.user,
            analytics.AnalyticsEvent.ROOM_CREATED,
            {
                "room_id": str(room.pk),
                "access_level": room.access_level,
                "client_id": client_id,
                "external_api": True,
                "auth_method": auth_method,
                "$set": {"email": self.request.user.email},
            },
        )
