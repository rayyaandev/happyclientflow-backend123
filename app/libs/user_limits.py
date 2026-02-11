"""
User Limits

Utility module for checking and enforcing user limits based on the company's
active subscription plan. Used by the invite creation flow and team management.
"""

from fastapi import HTTPException
from supabase import Client


async def check_user_limit(company_id: str, supabase_client: Client) -> dict:
    """
    Check if a company can add more users based on their subscription.

    Returns:
        {
            "allowed": bool,
            "max_users": int,
            "current_users": int,
            "plan_type": str | None,
            "included_users": int,
            "extra_seats": int,
            "reason": str | None,
        }
    """
    # Get active subscription
    sub_result = supabase_client.table('subscriptions').select(
        'plan_type, max_users, included_users, extra_seats'
    ).eq('company_id', company_id).in_('status', ['active', 'trialing']).maybe_single().execute()

    if not sub_result.data:
        return {
            "allowed": False,
            "reason": "no_subscription",
            "max_users": 0,
            "current_users": 0,
            "plan_type": None,
            "included_users": 0,
            "extra_seats": 0,
        }

    sub = sub_result.data
    max_users = sub.get('max_users') or (sub.get('included_users', 3) + sub.get('extra_seats', 0))

    # Count active users in this company
    users_result = supabase_client.table('users').select(
        'id', count='exact'
    ).eq('company_id', company_id).execute()

    # Count pending invites (they reserve a seat)
    invites_result = supabase_client.table('invites').select(
        'id', count='exact'
    ).eq('company_id', company_id).eq('status', 'Pending').execute()

    active_users = users_result.count or 0
    pending_invites = invites_result.count or 0
    current_total = active_users + pending_invites

    allowed = current_total < max_users

    return {
        "allowed": allowed,
        "max_users": max_users,
        "current_users": current_total,
        "plan_type": sub.get('plan_type'),
        "included_users": sub.get('included_users', 0),
        "extra_seats": sub.get('extra_seats', 0),
        "reason": None if allowed else "user_limit_reached",
    }


async def enforce_user_limit(company_id: str, supabase_client: Client) -> dict:
    """
    Raise HTTP 403 if user limit is reached for the company's plan.
    Returns the limit status dict if allowed.
    """
    result = await check_user_limit(company_id, supabase_client)

    if not result["allowed"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": result["reason"],
                "max_users": result["max_users"],
                "current_users": result["current_users"],
                "plan_type": result["plan_type"],
            }
        )

    return result
