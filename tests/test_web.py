"""Web panel: access control, pages, forms. The web process never talks FTP."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from lj_signage.extensions import db
from lj_signage.models import Assignment, AuditLog, Device, DeviceFile, Group, Job, User, Video
from lj_signage.timeutil import utcnow
from tests.conftest import ADMIN, EDITOR


def device(code: str) -> Device:
    return db.session.execute(db.select(Device).filter_by(code=code)).scalar_one()


def ready_video(title="Campanha Natal", sha="a" * 64, size=1000, duration=30.0) -> Video:
    video = Video(
        title=title,
        slug="campanha-natal",
        original_filename="natal.mov",
        sha256=sha,
        size_bytes=size,
        duration_s=duration,
        status="ready",
        width=1920,
        height=1080,
    )
    db.session.add(video)
    db.session.commit()
    return video


# --- access -----------------------------------------------------------------------------


def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_pages_require_login(client, synced):
    response = client.get("/")
    assert response.status_code == 302
    assert "/entrar" in response.headers["Location"]


def test_login_page_without_google_configuration(client):
    page = client.get("/entrar").get_data(as_text=True)
    assert "ainda não está configurado" in page
    assert client.get("/entrar/google").status_code == 404


def test_dev_login_is_off_unless_debug_and_enabled(client):
    assert client.post("/entrar/dev", data={"email": ADMIN}).status_code == 404


@pytest.fixture
def dev_app(app, settings):
    from dataclasses import replace

    app.config["LJ_SETTINGS"] = replace(settings, dev_login=True)
    app.debug = True
    yield app
    app.config["LJ_SETTINGS"] = settings
    app.debug = False


def test_admin_from_env_can_log_in(dev_app, client, synced):
    response = client.post("/entrar/dev", data={"email": ADMIN})
    assert response.status_code == 302
    assert client.get("/").status_code == 200
    assert db.session.execute(db.select(AuditLog).filter_by(action="login")).scalar_one()


def test_unauthorised_company_account_is_refused(dev_app, client, synced):
    client.post("/entrar/dev", data={"email": "loja04@lugardajoia.com"})
    assert client.get("/").status_code == 302
    denied = db.session.execute(db.select(AuditLog).filter_by(action="login_denied")).scalar_one()
    assert denied.result == "denied"


def test_other_domains_are_refused(dev_app, client, synced):
    client.post("/entrar/dev", data={"email": "alguem@gmail.com"})
    assert client.get("/").status_code == 302


def test_editor_authorised_by_admin(dev_app, client, synced):
    client.post("/entrar/dev", data={"email": ADMIN})
    client.post("/utilizadores", data={"email": "Designer@LugarDaJoia.com"})
    user = db.session.execute(
        db.select(User).filter_by(email="designer@lugardajoia.com")
    ).scalar_one()
    assert user.authorized and user.role == "editor"
    client.post("/sair")
    client.post("/entrar/dev", data={"email": "designer@lugardajoia.com"})
    assert client.get("/").status_code == 200


def test_only_company_accounts_can_be_authorised(admin_client):
    admin_client.post("/utilizadores", data={"email": "x@gmail.com"})
    assert db.session.execute(db.select(User).filter_by(email="x@gmail.com")).scalar() is None


def test_revoked_editor_is_logged_out_on_next_request(editor_client):
    assert editor_client.get("/").status_code == 200
    user = db.session.execute(db.select(User).filter_by(email=EDITOR)).scalar_one()
    user.authorized = False
    db.session.commit()
    assert editor_client.get("/").status_code == 302


def test_editors_cannot_use_admin_actions(editor_client):
    assert editor_client.get("/utilizadores").status_code == 403
    assert editor_client.post("/lojas/ubbo/descobrir").status_code == 403
    assert editor_client.post("/lojas/ubbo/pasta", data={"ftp_path": "/x"}).status_code == 403


def test_security_headers(admin_client):
    response = admin_client.get("/")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"


def test_csrf_is_enforced(app, admin_client):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = admin_client.post("/lojas/ubbo/aplicar")
        assert response.status_code == 400
        assert "expirou" in response.get_data(as_text=True)
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


# --- dashboard and stores ------------------------------------------------------------------


def test_dashboard_lists_the_stores(admin_client):
    page = admin_client.get("/").get_data(as_text=True)
    assert "Lugar da Jóia UBBO" in page and "Galeria da Jóia Porto" in page
    assert "Modo simulação" in page
    assert "Pasta de vídeos por confirmar" in page
    assert "Sincronização parada" in page  # no worker heartbeat in tests
    grid = admin_client.get("/painel/grelha")
    assert grid.status_code == 200 and 'id="store-grid"' in grid.get_data(as_text=True)


def with_snapshot(code="ubbo"):
    d = device(code)
    d.video_path = "/home/tmagalhaes/Videos"
    d.video_path_source = "discovery"
    d.snapshot_at = utcnow()
    d.status = "online"
    d.last_seen_at = utcnow()
    d.staging_exists = False
    db.session.add_all(
        [
            DeviceFile(device=d, location="root", remote_name="Promo Natal.mp4", size_bytes=10,
                       state="legacy", issues=["unsafe_name", "non_standard_name"]),
            DeviceFile(device=d, location="root", remote_name="clip.mp4", size_bytes=10,
                       state="legacy", issues=["non_standard_name"]),
        ]
    )  # fmt: skip
    db.session.commit()
    return d


def test_store_page_shows_files_plan_and_alerts(admin_client):
    with_snapshot()
    video = ready_video()
    db.session.add(
        Assignment(video=video, target_type="all", start_at=utcnow() - timedelta(hours=1))
    )
    db.session.commit()
    page = admin_client.get("/lojas/ubbo").get_data(as_text=True)
    assert "Promo Natal.mp4" in page and "clip.mp4" in page
    assert "Nome com espaços" in page
    assert "Enviar «Campanha Natal» para a preparação" in page
    assert "Sem vídeo de reserva" in page


def test_apply_now_creates_a_single_job(admin_client):
    with_snapshot()
    admin_client.post("/lojas/ubbo/aplicar", headers={"HX-Request": "true"})
    admin_client.post("/lojas/ubbo/aplicar")
    jobs = db.session.execute(db.select(Job).filter_by(type="reconcile")).scalars().all()
    assert len(jobs) == 1 and jobs[0].created_by == ADMIN
    assert admin_client.get("/lojas/ubbo/tarefas").status_code == 200


def test_apply_now_needs_a_confirmed_folder(admin_client):
    admin_client.post("/lojas/ubbo/aplicar")
    assert db.session.execute(db.select(Job)).scalar() is None


def test_confirm_path_only_accepts_discovered_candidates(admin_client):
    d = device("ubbo")
    d.discovery = {
        "status": "found",
        "candidates": [
            {"ftp_path": "/home/tmagalhaes/Videos", "source": "script", "evidence": ["x"],
             "video_count": 2, "other_count": 0, "sample": ["a.mp4"], "has_staging": False}
        ],
        "at": utcnow().isoformat(), "login_dir": "/home/tmagalhaes", "chroot": False,
        "notes": [], "error": None, "server": "vsFTPd 2.3.5",
    }  # fmt: skip
    db.session.commit()
    page = admin_client.get("/lojas/ubbo").get_data(as_text=True)
    assert "Confirmar esta pasta" in page and "vsFTPd 2.3.5" in page
    admin_client.post("/lojas/ubbo/pasta", data={"ftp_path": "/etc"})
    assert device("ubbo").video_path is None
    admin_client.post("/lojas/ubbo/pasta", data={"ftp_path": "/home/tmagalhaes/Videos"})
    d = device("ubbo")
    assert d.video_path == "/home/tmagalhaes/Videos"
    assert d.video_path_confirmed_by == ADMIN
    assert db.session.execute(db.select(Job).filter_by(type="reconcile")).scalar_one()


def test_legacy_delete_request_and_cancel(admin_client):
    d = with_snapshot()
    row = next(f for f in d.files if f.remote_name == "clip.mp4")
    admin_client.post(f"/lojas/ubbo/ficheiros/{row.id}/remover")
    assert row.delete_requested_by == ADMIN
    # clip.mp4 is the only playable video: deleting it would leave a black screen.
    page = admin_client.get("/plano/").get_data(as_text=True)
    assert "Apagar o ficheiro legado clip.mp4" not in page
    assert "Nada ficaria a passar" in page
    # Once something else plays, the deletion goes ahead (after the activation).
    db.session.add(Assignment(video=ready_video(), target_type="all", start_at=utcnow()))
    db.session.commit()
    page = admin_client.get("/plano/").get_data(as_text=True)
    assert "Apagar o ficheiro legado clip.mp4" in page
    admin_client.post(f"/lojas/ubbo/ficheiros/{row.id}/cancelar")
    assert row.delete_requested_at is None


def test_editor_can_adopt_but_not_delete_legacy(editor_client):
    d = with_snapshot()
    row = next(f for f in d.files if f.remote_name == "clip.mp4")
    assert editor_client.post(f"/lojas/ubbo/ficheiros/{row.id}/remover").status_code == 403
    editor_client.post(f"/lojas/ubbo/ficheiros/{row.id}/adotar")
    job = db.session.execute(db.select(Job).filter_by(type="adopt")).scalar_one()
    assert job.payload == {"file_id": row.id}


# --- library -----------------------------------------------------------------------------------


def test_upload_stores_the_original_and_queues_the_conversion(editor_client, settings):
    data = {"file": (io.BytesIO(b"\x00" * 2048), "Coleção Filigrana.mov")}
    response = editor_client.post(
        "/biblioteca/enviar", data=data, content_type="multipart/form-data"
    )
    assert response.status_code == 201
    video = db.session.execute(db.select(Video)).scalar_one()
    assert video.title == "Coleção Filigrana" and video.slug == "colecao-filigrana"
    assert (settings.data_dir / video.original_path).stat().st_size == 2048
    job = db.session.execute(db.select(Job).filter_by(type="transcode")).scalar_one()
    assert job.video_id == video.id and job.status == "pending"
    assert "hx-trigger" in response.get_data(as_text=True)  # the card polls its status


def test_upload_rejects_unknown_formats(editor_client):
    data = {"file": (io.BytesIO(b"x"), "apresentacao.pdf")}
    response = editor_client.post(
        "/biblioteca/enviar", data=data, content_type="multipart/form-data"
    )
    assert response.status_code == 400
    assert "não suportado" in response.get_data(as_text=True)


def test_library_pages(editor_client):
    video = ready_video()
    assert "Campanha Natal" in editor_client.get("/biblioteca/").get_data(as_text=True)
    assert editor_client.get(f"/biblioteca/{video.id}").status_code == 200
    assert editor_client.get(f"/biblioteca/{video.id}/estado").status_code == 200
    editor_client.post(f"/biblioteca/{video.id}", data={"title": "Natal 2026"})
    assert video.title == "Natal 2026"


def test_scheduled_video_cannot_be_deleted(editor_client):
    video = ready_video()
    db.session.add(Assignment(video=video, target_type="all", start_at=utcnow()))
    db.session.commit()
    editor_client.post(f"/biblioteca/{video.id}/apagar")
    assert video.deleted_at is None


def test_unscheduled_video_is_soft_deleted(editor_client):
    video = ready_video()
    editor_client.post(f"/biblioteca/{video.id}/apagar")
    assert video.deleted_at is not None
    assert editor_client.get(f"/biblioteca/{video.id}").status_code == 404


# --- schedule -------------------------------------------------------------------------------------


def form(video, **values):
    data = {
        "video_id": video.id,
        "target_type": "all",
        "group_id": 0,
        "device_id": 0,
        "start_at": "2026-10-01T09:00",
        "end_at": "",
        "position": 100,
        "note": "",
    }
    data.update(values)
    return data


def test_create_assignment_converts_local_time_to_utc(editor_client):
    video = ready_video()
    response = editor_client.post("/programacao/nova", data=form(video, end_at="2026-12-01T09:00"))
    assert response.status_code == 302
    a = db.session.execute(db.select(Assignment)).scalar_one()
    assert a.start_at.isoformat() == "2026-10-01T08:00:00+00:00"  # summer time
    assert a.end_at.isoformat() == "2026-12-01T09:00:00+00:00"  # winter time
    assert a.created_by == EDITOR


def test_nonexistent_spring_time_is_refused(editor_client):
    video = ready_video()
    response = editor_client.post(
        "/programacao/nova", data=form(video, start_at="2027-03-28T01:30")
    )
    assert response.status_code == 200
    assert "não existe" in response.get_data(as_text=True)
    assert db.session.execute(db.select(Assignment)).scalar() is None


def test_repeated_autumn_hour_uses_the_first_occurrence(editor_client):
    video = ready_video()
    editor_client.post("/programacao/nova", data=form(video, start_at="2026-10-25T01:30"))
    a = db.session.execute(db.select(Assignment)).scalar_one()
    assert a.start_at.isoformat() == "2026-10-25T00:30:00+00:00"
    page = editor_client.get("/programacao/").get_data(as_text=True)
    assert "acontece duas vezes" in page


def test_group_and_store_targets(editor_client):
    video = ready_video()
    group = db.session.execute(db.select(Group).filter_by(name="norte")).scalar_one()
    editor_client.post(
        "/programacao/nova", data=form(video, target_type="group", group_id=group.id)
    )
    editor_client.post(
        "/programacao/nova", data=form(video, target_type="device", device_id=device("al").id)
    )
    targets = sorted(
        (a.target_type, a.target_id) for a in db.session.execute(db.select(Assignment)).scalars()
    )
    assert targets == sorted([("group", group.id), ("device", device("al").id)])
    page = editor_client.get("/programacao/").get_data(as_text=True)
    assert "Grupo norte" in page and "05 · Lugar da Jóia Alameda" in page


def test_invalid_assignment_forms(editor_client):
    video = ready_video()
    editor_client.post("/programacao/nova", data=form(video, target_type="group"))
    editor_client.post("/programacao/nova", data=form(video, end_at="2026-09-01T09:00"))
    assert db.session.execute(db.select(Assignment)).scalar() is None


def test_edit_end_and_delete_assignment(editor_client):
    video = ready_video()
    a = Assignment(video=video, target_type="all", start_at=utcnow() - timedelta(days=1))
    db.session.add(a)
    db.session.commit()
    assert editor_client.get(f"/programacao/{a.id}/editar").status_code == 200
    editor_client.post(f"/programacao/{a.id}/editar", data=form(video, position=5))
    assert a.position == 5
    editor_client.post(f"/programacao/{a.id}/terminar")
    assert a.end_at is not None and a.end_at <= utcnow()
    editor_client.post(f"/programacao/{a.id}/apagar")
    assert db.session.get(Assignment, a.id) is None
    actions = {e.action for e in db.session.execute(db.select(AuditLog)).scalars()}
    assert {"assignment_updated", "assignment_ended", "assignment_deleted"} <= actions


def test_schedule_list_calendar_and_fallback_warning(editor_client):
    video = ready_video()
    db.session.add(
        Assignment(video=video, target_type="all", start_at=utcnow() - timedelta(hours=1))
    )
    db.session.commit()
    page = editor_client.get("/programacao/").get_data(as_text=True)
    assert "Não há nenhum vídeo de reserva" in page
    assert "Entra no ar até" not in page  # simulation: nothing is sent
    calendar = editor_client.get("/programacao/calendario?mes=2026-10")
    assert calendar.status_code == 200
    assert "outubro de 2026" in calendar.get_data(as_text=True)
    assert editor_client.get("/programacao/calendario?mes=xx&loja=ubbo").status_code == 200


# --- plan, activity, api ---------------------------------------------------------------------


def test_plan_and_activity_pages(admin_client):
    with_snapshot()
    assert "Plano" in admin_client.get("/plano/").get_data(as_text=True)
    admin_client.post("/lojas/ubbo/aplicar")
    page = admin_client.get("/atividade/?acao=reconcile_requested&loja=ubbo").get_data(as_text=True)
    assert "Aplicar agora" in page
    empty = admin_client.get("/atividade/?utilizador=sistema&de=2020-01-01&ate=2020-01-02")
    assert "Sem registos" in empty.get_data(as_text=True)


def test_status_api(admin_client):
    data = admin_client.get("/api/estado").json
    assert data["dry_run"] is True
    assert [d["code"] for d in data["devices"]] == ["ubbo", "gjpt", "al"]


def test_on_air_estimate_when_writing_is_enabled(app, editor_client, settings):
    from dataclasses import replace

    from lj_signage.status import write_heartbeat

    video = ready_video()
    db.session.add(Assignment(video=video, target_type="all", start_at=utcnow()))
    d = with_snapshot("ubbo")
    d.enabled = True
    db.session.commit()
    write_heartbeat(
        worker_id="w", started_at=utcnow(), next_reconcile_at=utcnow() + timedelta(minutes=3),
        dry_run=False,
    )  # fmt: skip
    app.config["LJ_SETTINGS"] = replace(settings, dry_run=False)
    try:
        page = editor_client.get("/programacao/").get_data(as_text=True)
    finally:
        app.config["LJ_SETTINGS"] = settings
    assert "Entra no ar até" in page
