"""FastAPI web dashboard for the giveaway bot."""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from bot.models.database import async_session, engine, init_db
from bot.models.giveaway import Giveaway, GiveawayStatus, GiveawayParticipant, GiveawayWinner
from bot.models.contest import Contest, ContestStatus, ContestSubmission, ContestVote
from bot.models.loyalty import LoyaltyPoints
from bot.models.referral import Referral
from bot.models.moderation import Blacklist, ContentFlag

BASE_DIR = Path(__file__).parent
app = FastAPI(title="QurachiBot Dashboard", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Register Mini App API routes
from web.miniapp_api import router as miniapp_router
app.include_router(miniapp_router)


@app.on_event("startup")
async def startup():
    await init_db()


@app.get("/miniapp/giveaway", response_class=HTMLResponse)
async def miniapp_giveaway_page(request: Request, id: int = 0):
    """Serve the Mini App HTML for giveaway participation."""
    return templates.TemplateResponse("miniapp/giveaway.html", {"request": request})


@app.get("/miniapp", response_class=HTMLResponse)
async def miniapp_main(request: Request, tab: str = "home"):
    """Serve the full Mini App (main tabs: home, games, leaders, create)."""
    return templates.TemplateResponse("miniapp/app.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page with stats."""
    async with async_session() as session:
        # Giveaway stats
        gw_total = (await session.execute(select(func.count(Giveaway.id)))).scalar()
        gw_active = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == "active")
        )).scalar()
        gw_completed = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == "completed")
        )).scalar()
        total_participants = (await session.execute(select(func.count(GiveawayParticipant.id)))).scalar()
        total_winners = (await session.execute(select(func.count(GiveawayWinner.id)))).scalar()

        # Contest stats
        ct_total = (await session.execute(select(func.count(Contest.id)))).scalar()
        ct_active = (await session.execute(
            select(func.count(Contest.id)).where(Contest.status == ContestStatus.ACCEPTING_SUBMISSIONS)
        )).scalar()
        total_submissions = (await session.execute(select(func.count(ContestSubmission.id)))).scalar()
        total_votes = (await session.execute(select(func.count(ContestVote.id)))).scalar()

        # User stats
        unique_users = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
        )).scalar()
        total_referrals = (await session.execute(select(func.count(Referral.id)))).scalar()

        # Moderation
        blacklisted = (await session.execute(
            select(func.count(Blacklist.id)).where(Blacklist.is_active == True)
        )).scalar()
        flagged = (await session.execute(
            select(func.count(ContentFlag.id)).where(ContentFlag.resolved == False)
        )).scalar()

        # Recent giveaways
        result = await session.execute(
            select(Giveaway).options(selectinload(Giveaway.participants))
            .order_by(Giveaway.created_at.desc()).limit(5)
        )
        recent_giveaways = result.scalars().all()

        # Recent contests
        result = await session.execute(
            select(Contest).options(selectinload(Contest.submissions))
            .order_by(Contest.created_at.desc()).limit(5)
        )
        recent_contests = result.scalars().all()

        # Leaderboard top 10
        result = await session.execute(
            select(LoyaltyPoints).order_by(LoyaltyPoints.total_earned.desc()).limit(10)
        )
        leaderboard = result.scalars().all()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "gw_total": gw_total,
        "gw_active": gw_active,
        "gw_completed": gw_completed,
        "total_participants": total_participants,
        "total_winners": total_winners,
        "ct_total": ct_total,
        "ct_active": ct_active,
        "total_submissions": total_submissions,
        "total_votes": total_votes,
        "unique_users": unique_users,
        "total_referrals": total_referrals,
        "blacklisted": blacklisted,
        "flagged": flagged,
        "recent_giveaways": recent_giveaways,
        "recent_contests": recent_contests,
        "leaderboard": leaderboard,
        "now": datetime.utcnow(),
    })


@app.get("/api/stats")
async def api_stats():
    """JSON API for stats (for live updates)."""
    async with async_session() as session:
        gw_total = (await session.execute(select(func.count(Giveaway.id)))).scalar()
        gw_active = (await session.execute(
            select(func.count(Giveaway.id)).where(Giveaway.status == "active")
        )).scalar()
        total_participants = (await session.execute(select(func.count(GiveawayParticipant.id)))).scalar()
        ct_total = (await session.execute(select(func.count(Contest.id)))).scalar()
        total_submissions = (await session.execute(select(func.count(ContestSubmission.id)))).scalar()
        total_votes = (await session.execute(select(func.count(ContestVote.id)))).scalar()
        unique_users = (await session.execute(
            select(func.count(func.distinct(GiveawayParticipant.user_id)))
        )).scalar()
        total_referrals = (await session.execute(select(func.count(Referral.id)))).scalar()

    return {
        "giveaways": {"total": gw_total, "active": gw_active, "participants": total_participants},
        "contests": {"total": ct_total, "submissions": total_submissions, "votes": total_votes},
        "users": {"unique": unique_users, "referrals": total_referrals},
    }


@app.get("/api/leaderboard")
async def api_leaderboard():
    """JSON API for leaderboard."""
    async with async_session() as session:
        result = await session.execute(
            select(LoyaltyPoints).order_by(LoyaltyPoints.total_earned.desc()).limit(20)
        )
        users = result.scalars().all()

    return [
        {
            "rank": i + 1,
            "username": u.username,
            "first_name": u.first_name,
            "points": u.total_earned,
            "wins": u.wins,
            "referrals": u.referrals_made,
        }
        for i, u in enumerate(users)
    ]


@app.get("/api/giveaways")
async def api_giveaways():
    """JSON API for giveaways list."""
    async with async_session() as session:
        result = await session.execute(
            select(Giveaway).options(selectinload(Giveaway.participants))
            .order_by(Giveaway.created_at.desc()).limit(20)
        )
        giveaways = result.scalars().all()

    return [
        {
            "id": gw.id,
            "title": gw.title,
            "prize": gw.prize,
            "status": gw.status.value,
            "participants": len(gw.participants),
            "winner_count": gw.winner_count,
            "created_at": gw.created_at.isoformat() if gw.created_at else None,
            "ends_at": gw.ends_at.isoformat() if gw.ends_at else None,
        }
        for gw in giveaways
    ]


@app.get("/api/contests")
async def api_contests():
    """JSON API for contests list."""
    async with async_session() as session:
        result = await session.execute(
            select(Contest).options(selectinload(Contest.submissions))
            .order_by(Contest.created_at.desc()).limit(20)
        )
        contests = result.scalars().all()

    return [
        {
            "id": ct.id,
            "title": ct.title,
            "status": ct.status.value,
            "type": ct.contest_type.value,
            "submissions": len(ct.submissions),
            "prize": ct.prize,
            "created_at": ct.created_at.isoformat() if ct.created_at else None,
        }
        for ct in contests
    ]
