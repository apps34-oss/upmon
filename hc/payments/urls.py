from __future__ import annotations

from django.urls import path

from . import views

urlpatterns = [
    path("projects/<uuid:code>/pricing/", views.pricing, name="hc-p-pricing"),
    path("pricing/", views.pricing, name="hc-pricing"),
    path("accounts/profile/billing/", views.billing, name="hc-billing"),
    path("checkout/", views.create_checkout_session, name="hc-checkout"),
    path("portal/", views.create_customer_portal_session, name="hc-customer-portal"),
    path("webhook/subscription/", views.subscription_webhook, name="hc-subscription-webhook"),
]
