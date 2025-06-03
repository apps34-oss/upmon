from __future__ import annotations

from typing import Any, Dict
from uuid import UUID

import hmac
import os
import json
import logging
from hashlib import sha256

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from hc.accounts.http import AuthenticatedHttpRequest
from hc.accounts.models import Profile
from hc.front.views import _get_project_for_user
from hc.payments.models import Subscription

import datetime
from django.utils import timezone

logger = logging.getLogger(__name__)


def _make_apps34_request(url: str, payload: Dict[str, Any]) -> Any:
    """Make authenticated request to stripe.apps34.com"""
    import requests  # type: ignore
    
    # Generate signature
    apps34_secret = os.getenv("APPS34_SECRET", "---")

    payload_json = json.dumps(payload, sort_keys=True)
    signature = hmac.new(
        apps34_secret.encode(),
        payload_json.encode(),
        sha256
    ).hexdigest()
    
    headers = {
        "X-Signature": signature,
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        return response
    except Exception as e:
        logger.error(f"Error making request to {url}: {e}")
        # Return a mock response object with status_code attribute
        class ErrorResponse:
            status_code = 500
            def json(self) -> Dict[str, Any]:
                return {}
        return ErrorResponse()


def pricing(request: HttpRequest, code: UUID | None = None) -> HttpResponse:
    project = None
    if code:
        if not request.user.is_authenticated:
            raise Http404()

        # code is already a UUID from the URL pattern
        project, rw = _get_project_for_user(request, code)
        if project.owner != request.user:
            ctx = {"page": "pricing", "project": project}
            return render(request, "payments/pricing_not_owner.html", ctx)

    sub = None
    if request.user.is_authenticated:
        # Don't use Subscription.objects.for_user method here, so a
        # subscription object is not created just by viewing a page.
        sub = Subscription.objects.filter(user_id=request.user.id).first()

    ctx = {"page": "pricing", "project": project, "sub": sub}
    return render(request, "payments/pricing.html", ctx)

@login_required
def billing(request: AuthenticatedHttpRequest) -> HttpResponse:
    # Don't use Subscription.objects.for_user method here, so a
    # subscription object is not created just by viewing a page.
    user_id = request.user.id
    if user_id is not None:
        sub = Subscription.objects.filter(user_id=user_id).first()
        if sub is None:
            # Since we're in a login_required view, we know this is an authenticated user
            sub = Subscription(user=request.user)
    else:
        sub = None

    ctx = {
        "page": "billing",
        "profile": request.profile,
        "sub": sub,
    }

    return render(request, "accounts/billing.html", ctx)


@login_required
def create_customer_portal_session(request: AuthenticatedHttpRequest) -> HttpResponse:
    from django.shortcuts import redirect
    
    user_id = request.user.id
    user_email = request.user.email
    
    url = "https://stripe.apps34.com/create-customer-portal-session"
    payload = {
        "service": "upmon",
        "userId": user_id,
        "userEmail": user_email
    }
    
    if "update" in request.GET:
        payload["update"] = request.GET["update"]
    
    response = _make_apps34_request(url, payload)
    
    if response.status_code == 200:
        data = response.json()
        if "url" in data:
            return redirect(data["url"])
        else:
            return HttpResponse("Invalid response from payment service", status=500)
    else:
        return HttpResponse(f"Payment service error: {response.status_code}", status=500)


@login_required
def create_checkout_session(request: AuthenticatedHttpRequest) -> HttpResponse:
    from django.shortcuts import redirect
    
    plan_id = request.GET.get("plan", "")
    if not plan_id:
        return HttpResponse("Missing plan parameter", status=400)
    
    user_id = request.user.id
    user_email = request.user.email
    
    url = "https://stripe.apps34.com/create-checkout-session"
    payload = {
        "service": "upmon",
        "planId": plan_id,
        "userId": user_id,
        "userEmail": user_email
    }
    
    if "success_url" in request.GET:
        payload["success_url"] = request.GET["success_url"]
    if "cancel_url" in request.GET:
        payload["cancel_url"] = request.GET["cancel_url"]
    
    response = _make_apps34_request(url, payload)
    
    if response.status_code == 200:
        data = response.json()
        if "url" in data:
            return redirect(data["url"])
        else:
            return HttpResponse("Invalid response from payment service", status=500)
    else:
        return HttpResponse(f"Payment service error: {response.status_code}", status=500)


@csrf_exempt
@require_POST
def subscription_webhook(request: HttpRequest) -> HttpResponse:
    try:
        data = json.loads(request.body.decode("utf-8"))
        logger.info(f"Received subscription webhook: {data.get('id', 'unknown-id')}")

        # Extract the essential fields
        subscription_id = data.get("id", "")
        status = data.get("status", "")
        customer = data.get("customer", "")
        start_date = data.get("startDate")
        period_start = data.get("periodStart")
        period_end = data.get("periodEnd")
        user_id = data.get("userId", "")
        user_email = data.get("userEmail", "")
        plan_id = data.get("planId", "")

        # Validate required fields
        if not subscription_id or not status or not user_id:
            logger.error("Missing required fields in webhook payload")
            return HttpResponse("Missing required fields", status=400)

        # Find the user by ID
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            logger.error(f"User not found: {user_id}")
            return HttpResponse("User not found", status=400)

        # Get or create subscription for user
        subscription, created = Subscription.objects.get_or_create(user=user)
        
        # Get user's profile
        profile = Profile.objects.for_user(user)
        
        # Update the subscription based on status
        if status == "active" or status == "past_due":
            subscription.subscription_id = subscription_id[:10]
            subscription.customer_id = customer

            # Determine plan ID and update limits based on plan
            if plan_id == "business":
                plan_name = "Business"
                profile.check_limit = 100
                profile.team_limit = 10
                profile.ping_log_limit = 1000
                profile.sms_limit = 30
                profile.call_limit = 10
            elif plan_id == "pro":
                plan_name = "Pro"
                profile.check_limit = 20
                profile.team_limit = 3
                profile.ping_log_limit = 1000
                profile.sms_limit = 5
                profile.call_limit = 2
            else:
                # Free plan or unknown plan
                plan_id = ""
                plan_name = ""
                profile.check_limit = 5
                profile.team_limit = 1
                profile.ping_log_limit = 20
                profile.sms_limit = 0
                profile.call_limit = 0

            subscription.plan_id = plan_id
            subscription.plan_name = plan_name

            subscription.next_billing_date = datetime.date.fromtimestamp(period_end)

            logger.info(f"Updated subscription for user: {user.email}, plan: {plan_id}")

        else:
            # Cancel the subscription - revert to free plan limits
            subscription.subscription_id = ""
            subscription.plan_id = ""
            subscription.plan_name = ""
            
            profile.check_limit = 5
            profile.team_limit = 1
            profile.ping_log_limit = 20
            profile.sms_limit = 0
            profile.call_limit = 0
            
            logger.info(f"Cancelled subscription for user: {user.email}")

        # Save changes
        subscription.save()
        profile.save()

    except json.JSONDecodeError:
        logger.error("Invalid JSON payload")
        return HttpResponse("Invalid JSON", status=400)
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return HttpResponse("Internal error", status=500)
    
    return HttpResponse("OK", status=200)
