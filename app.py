import zipfile
import os
import csv
import random
import io
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, Response, session
)
from flask_sqlalchemy import SQLAlchemy

# ----------------- Flask初期化 -----------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "secret")

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///exam.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ----------------- DBモデル -----------------
class Exam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    duration = db.Column(db.Integer, nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    pass_rate = db.Column(db.Float, default=0.6)
    exam_date = db.Column(db.String(20))
    mode = db.Column(db.String(20), default="exam")  # exam / study

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False)
    question_text = db.Column(db.String(500), nullable=False)
    choices = db.Column(db.String(500), nullable=False)
    correct_answer = db.Column(db.String(100), nullable=False)
    explanation = db.Column(db.Text)   # ← ★追加

class Answer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_name = db.Column(db.String(100), nullable=False)
    exam_id = db.Column(db.Integer, nullable=False)
    question_id = db.Column(db.Integer, nullable=False)
    selected_answer = db.Column(db.String(100)) 

    # ----------------- 合否判定 -----------------
def is_pass(score, total, pass_rate):
    if total == 0:
        return False
    return (score / total) >= pass_rate   

## ----------------- CSV読み込み -----------------
def load_questions_from_csv_obj(file_obj, exam_id):
    file_obj.seek(0)
    text_file = io.TextIOWrapper(file_obj, encoding="utf-8-sig")
    reader = csv.reader(text_file)
    next(reader, None)

    for row in reader:
        if not row:
            continue

        # ★ 必ず12列に揃える（A～L）
        row = row + [""] * (12 - len(row))

        question_text = row[0].strip()

        # B～J（1～9列目）を選択肢にする
        choices_list = [
            row[i].strip()
            for i in range(1, 10)
            if row[i].strip()
        ]

        correct_answer = row[10].strip()  # K列
        explanation = row[11].strip()     # L列

        if not question_text or not correct_answer:
            continue

        if len(choices_list) < 2:
            continue

        # 正解が選択肢に含まれていなければ追加
        if correct_answer not in choices_list:
            choices_list.append(correct_answer)

        q = Question(
            exam_id=exam_id,
            question_text=question_text,
            choices="|".join(choices_list),
            correct_answer=correct_answer,
            explanation=explanation
        )
        db.session.add(q)

    db.session.commit()
# ----------------- 管理者チェック -----------------
def require_admin():
    return session.get("admin") is True

# ----------------- 受験者側 -----------------
@app.route("/")
def index_page():
    exams = Exam.query.all()
    return render_template("index.html", exams=exams)

@app.route("/start_exam", methods=["POST"])
def start_exam():
    student_name = request.form.get("student_name", "").strip()
    exam_id = request.form.get("exam_id")
    mode = request.form.get("mode", "exam")

    if not student_name or not exam_id:
        flash("お名前と試験を選択してください")
        return redirect(url_for("index_page"))

    exam = db.session.get(Exam, exam_id)
    if not exam:
        flash("選択した試験は存在しません")
        return redirect(url_for("index_page"))

    session[f"exam_{exam_id}_mode_{student_name}"] = mode
    session_key = f"exam_{exam_id}_start_time_{student_name}"

    if session_key not in session:
        session[session_key] = datetime.now(timezone.utc).timestamp()

    remaining_seconds = None
    if mode == "exam":
        remaining_seconds = session.get(
            f"exam_{exam_id}_remaining_time_{student_name}"
        )
        if remaining_seconds is None:
            total_time = exam.duration * 60
            elapsed_time = int(
                datetime.now(timezone.utc).timestamp()
                - session[session_key]
            )
            remaining_seconds = max(total_time - elapsed_time, 0)

    return render_template(
        "exam.html",
        student_name=student_name,
        exam_id=exam_id,
        remaining_seconds=remaining_seconds,
        mode=mode
    )

@app.route("/load_questions")
def load_exam_questions():
    exam_id = request.args.get("exam_id")
    student_name = request.args.get("student_name", "")

    exam = db.session.get(Exam, exam_id)
    if not exam:
        return jsonify({"questions": [], "effective_total": 0})

    question_ids = session.get(
        f"exam_{exam_id}_question_ids_{student_name}", None
    )

    if not question_ids:
        questions = Question.query.filter_by(exam_id=exam_id).all()
        selected = random.sample(
            questions, min(exam.total_questions, len(questions))
        )
        question_ids = [q.id for q in selected]

        session[f"exam_{exam_id}_question_ids_{student_name}"] = question_ids
        session.modified = True

    questions = Question.query.filter(
        Question.id.in_(question_ids)
    ).all()

    questions = sorted(
        questions, key=lambda q: question_ids.index(q.id)
    )

    mode = session.get(
        f"exam_{exam_id}_mode_{student_name}", "exam"
    )

    # ⭐追加
    remaining_seconds = session.get(
        f"exam_{exam_id}_remaining_time_{student_name}"
    )

    return jsonify({
        "questions": [dict(
            id=q.id,
            question_text=q.question_text,
            choices=[c for c in q.choices.split("|") if c.strip()]
        ) for q in questions],
        "effective_total": len(question_ids),
        "mode": mode,
        "remaining_seconds": remaining_seconds   # ⭐追加
    })

@app.route("/save_remaining_time", methods=["POST"])
def save_remaining_time():
    data = request.get_json()
    exam_id = data.get("exam_id")
    student_name = data.get("student_name")
    remaining_seconds = data.get("remaining_seconds")
    session[f"exam_{exam_id}_remaining_time_{student_name}"] = remaining_seconds
    return jsonify({"status": "ok"})

@app.route("/check_answer", methods=["POST"])
def check_answer():
    data = request.get_json()
    exam_id = data.get("exam_id")
    question_id = data.get("question_id")
    selected = data.get("selected_answer", "").strip()
    student_name = data.get("student_name", "").strip()

    exam = db.session.get(Exam, exam_id)
    question = db.session.get(Question, question_id)
    if not exam or not question or question.exam_id != exam.id:
        return jsonify({"error": "invalid"}), 400

    # 回答保存
    answers = session.get(
        f"exam_{exam_id}_answers_{student_name}", {}
    )

    answers[str(question_id)] = selected

    session[f"exam_{exam_id}_answers_{student_name}"] = answers
    session[f"exam_{exam_id}_in_progress_{student_name}"] = True

    # ★重要
    session.modified = True

    # 正誤判定
    is_correct = (
        selected.lower()
        == question.correct_answer.strip().lower()
    )

    mode = session.get(
        f"exam_{exam_id}_mode_{student_name}", "exam"
    )

    if mode == "study":
        correct_answer = question.correct_answer
    else:
        correct_answer = None if is_correct else question.correct_answer

    return jsonify({
        "is_correct": is_correct,
        "correct_answer": correct_answer,
        "explanation": question.explanation or ""
    })

# ----------------- 提出・終了処理 -----------------
@app.route("/submit_exam", methods=["POST"])
def submit_exam():
    data = request.get_json()
    student_name = data.get("student_name", "").strip()
    exam_id = data.get("exam_id")

    answers = session.get(
        f"exam_{exam_id}_answers_{student_name}", {}
    )

    # ★修正：question_ids も受験者別キー
    question_ids = session.get(
        f"exam_{exam_id}_question_ids_{student_name}", []
    )

    if not question_ids:
        flash("試験データが不正です")
        return jsonify({"status": "error"})

    if not answers:
        answers = {str(qid): "未回答" for qid in question_ids}

    mode = session.get(
        f"exam_{exam_id}_mode_{student_name}", "exam"
    )

    if mode != "study":
        Answer.query.filter_by(
            student_name=student_name,
            exam_id=exam_id
        ).delete()

        for qid in question_ids:
            selected = answers.get(str(qid), "未回答")
            db.session.add(Answer(
                student_name=student_name,
                exam_id=exam_id,
                question_id=qid,
                selected_answer=selected
            ))
        db.session.commit()

    # ★修正：question_ids の削除キーも統一
    keys_to_clear = [
        f"exam_{exam_id}_start_time_{student_name}",
        f"exam_{exam_id}_question_ids_{student_name}",
        f"exam_{exam_id}_mode_{student_name}",
        f"exam_{exam_id}_answers_{student_name}",
        f"exam_{exam_id}_in_progress_{student_name}",
        f"exam_{exam_id}_remaining_time_{student_name}"
    ]
    for k in keys_to_clear:
        session.pop(k, None)

    return jsonify({"status": "ok"})

# （以降の管理者・CSV・ZIP機能は **元コードから一切変更なし**）
# ↓↓↓ この下はそのまま ↓↓↓



@app.route("/result")
def result():
    student_name = request.args.get("student_name", "").strip()
    exam_id = request.args.get("exam_id")

    answers = Answer.query.filter_by(student_name=student_name, exam_id=exam_id).all()
    if not answers:
        flash("結果が見つかりません")
        return redirect(url_for("index_page"))

    answer_map = {a.question_id: a.selected_answer for a in answers}
    question_ids = [a.question_id for a in answers]
    questions = Question.query.filter(Question.id.in_(question_ids)).all()

    correct = 0
    results = []
    for q in questions:
        sel = answer_map.get(q.id, "未回答")
        ok = sel != "未回答" and sel.strip().lower() == q.correct_answer.strip().lower()
        if ok:
            correct += 1
        results.append(dict(
            question_text=q.question_text,
            selected_answer=sel,
            correct_answer=q.correct_answer,
            is_correct=ok
        ))

    exam = db.session.get(Exam, exam_id)
    status = "合格" if is_pass(correct, len(questions), exam.pass_rate) else "不合格"

    return render_template(
        "result.html",
        student_name=student_name,
        total=correct,
        max_score=len(questions),
        question_results=results,
        status=status
    )

# ----------------- 管理者 -----------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == os.environ.get("ADMIN_PASSWORD", "admin"):
            session["admin"] = True
            flash("管理者ログイン成功")
            return redirect(url_for("admin"))
        flash("パスワードが違います")
        return redirect(url_for("admin"))

    if not require_admin():
        return render_template("admin_login.html")

    exams = Exam.query.all()
    history = []

    for exam in exams:
        answers = Answer.query.filter_by(exam_id=exam.id).all()
        by_student = {}
        for a in answers:
            by_student.setdefault(a.student_name, []).append(a)

        correct_map = {q.id: q.correct_answer for q in Question.query.filter_by(exam_id=exam.id).all()}

        for student, ans_list in by_student.items():
            score = sum(
                1 for a in ans_list
                if a.selected_answer != "未回答" and a.selected_answer.strip().lower() == correct_map[a.question_id].strip().lower()
            )
            history.append(dict(
                exam_id=exam.id,
                exam_name=exam.name,
                student_name=student,
                score=f"{score}/{len(ans_list)}",
                status="合格" if is_pass(score, len(ans_list), exam.pass_rate) else "不合格",
                exam_date=exam.exam_date
            ))

    return render_template("admin.html", exams=exams, exam_history=history)

@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    if not require_admin():
        flash("管理者ログインが必要です")
        return redirect(url_for("admin"))

    name = request.form.get("exam_name", "").strip()
    duration = request.form.get("duration", type=int)
    total_questions = request.form.get("total_questions", type=int)
    pass_rate_percent = request.form.get("pass_rate", type=float, default=60.0)
    exam_date = request.form.get("exam_date", "").strip()
    mode = request.form.get("mode", "exam")

    if not name or not duration or not total_questions:
        flash("試験名・制限時間・問題数は必須です")
        return redirect(url_for("admin"))

    pass_rate = pass_rate_percent / 100.0

    exam = Exam(
        name=name,
        duration=duration,
        total_questions=total_questions,
        pass_rate=pass_rate,
        exam_date=exam_date,
        mode=mode
    )
    db.session.add(exam)
    db.session.commit()

    file = request.files.get("csv_file")
    if file:
        load_questions_from_csv_obj(file, exam.id)
        flash(f"試験 {exam.name} を登録しました")
    else:
        flash("CSVファイルがアップロードされていません")

    return redirect(url_for("admin"))

# ★ CSV出力（BOM付き UTF-8）
@app.route("/admin/export/<int:exam_id>")
def export_exam_csv(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    questions = Question.query.filter_by(exam_id=exam_id).all()

    output = io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM

    writer = csv.writer(output)
    writer.writerow([
        "問題ID",
        "問題文",
        "選択肢",
        "正解",
    ])
    for q in questions:
        writer.writerow([
            q.id,
            q.question_text,
            " | ".join(q.choices.split("|")),
            q.correct_answer
        ])

    response = Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8"
    )
    response.headers["Content-Disposition"] = f"attachment; filename=exam_{exam_id}.csv"
    return response

@app.route("/admin/delete_exam/<int:exam_id>", methods=["POST"])
def delete_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    Question.query.filter_by(exam_id=exam_id).delete()
    Answer.query.filter_by(exam_id=exam_id).delete()
    db.session.delete(exam)
    db.session.commit()
    return redirect(url_for("admin"))

# ★ 受験者名日本語対応の回答削除（POSTフォーム対応）
@app.route("/admin/delete_answer", methods=["POST"])
def delete_answer():
    if not require_admin():
        flash("管理者ログインが必要です")
        return redirect(url_for("admin"))

    exam_id = request.form.get("exam_id", type=int)
    student_name = request.form.get("student_name", "").strip()
    if not exam_id or not student_name:
        flash("試験IDまたは受験者名が指定されていません")
        return redirect(url_for("admin"))

    Answer.query.filter_by(exam_id=exam_id, student_name=student_name).delete()
    db.session.commit()
    flash(f"{student_name} さんの回答を削除しました")
    return redirect(url_for("admin"))

# ★ 管理者ログアウト
@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    flash("管理者をログアウトしました")
    return redirect(url_for("admin"))

# =====================================================
# ★追加：受験結果CSV出力（試験ごと・複数人）
# =====================================================
@app.route("/admin/export_results/<int:exam_id>")
def export_exam_results(exam_id):
    if not require_admin():
        flash("管理者ログインが必要です")
        return redirect(url_for("admin"))

    exam = Exam.query.get_or_404(exam_id)
    answers = Answer.query.filter_by(exam_id=exam_id).all()
    questions = Question.query.filter_by(exam_id=exam_id).all()

    correct_map = {q.id: q.correct_answer for q in questions}

    by_student = {}
    for a in answers:
        by_student.setdefault(a.student_name, []).append(a)

    output = io.StringIO()
    output.write('\ufeff')  # ★追加：BOM付き
    writer = csv.writer(output)

    writer.writerow([
        "試験名",
        "受験者名",
        "試験日",
        "点数",
        "満点",
        "合否"
    ])  # ★追加

    for student, ans_list in by_student.items():
        score = sum(
            1 for a in ans_list
            if a.selected_answer != "未回答"
            and a.selected_answer.strip().lower()
            == correct_map[a.question_id].strip().lower()
        )
        status = "合格" if is_pass(score, len(ans_list), exam.pass_rate) else "不合格"

        writer.writerow([
            exam.name,
            student,
            exam.exam_date,
            score,
            len(ans_list),
            status
        ])  # ★追加

    response = Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8"
    )
    response.headers["Content-Disposition"] = \
        f"attachment; filename=exam_{exam_id}_results.csv"  # ★追加
    return response

    # =====================================================
# ★ 各受験者 × 各問題の正誤 CSV 出力
# =====================================================
@app.route("/admin/export_results_detail/<int:exam_id>")
def export_exam_results_detail(exam_id):
    if not require_admin():
        flash("管理者ログインが必要です")
        return redirect(url_for("admin"))

    exam = Exam.query.get_or_404(exam_id)

    questions = Question.query.filter_by(exam_id=exam_id).all()
    answers = Answer.query.filter_by(exam_id=exam_id).all()

    question_map = {q.id: q for q in questions}

    output = io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM
    writer = csv.writer(output)

    # ヘッダー
    writer.writerow([
        "試験名",
        "受験者名",
        "問題ID",
        "問題文",
        "選択肢",
        "正解",
        "受験者の回答",
        "正誤"
    ])

    for a in answers:
        q = question_map.get(a.question_id)
        if not q:
            continue

        is_correct = (
            a.selected_answer != "未回答"
            and a.selected_answer.strip().lower()
            == q.correct_answer.strip().lower()
        )

        writer.writerow([
            exam.name,
            a.student_name,
            q.id,
            q.question_text,
            " | ".join(q.choices.split("|")),
            q.correct_answer,
            a.selected_answer,
            "正解" if is_correct else "不正解"
        ])

    response = Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8"
    )
    response.headers["Content-Disposition"] = \
        f"attachment; filename=exam_{exam_id}_detail_results.csv"

    return response

# =====================================================
# ★ 受験者別 正誤CSV（ZIPで一括出力）
# =====================================================
@app.route("/admin/export_results_by_student/<int:exam_id>")
def export_results_by_student(exam_id):
    if not require_admin():
        flash("管理者ログインが必要です")
        return redirect(url_for("admin"))

    exam = Exam.query.get_or_404(exam_id)

    questions = Question.query.filter_by(exam_id=exam_id).all()
    answers = Answer.query.filter_by(exam_id=exam_id).all()

    question_map = {q.id: q for q in questions}

    # 受験者ごとにまとめる
    by_student = {}
    for a in answers:
        by_student.setdefault(a.student_name, []).append(a)

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for student, ans_list in by_student.items():
            csv_output = io.StringIO()
            csv_output.write('\ufeff')  # BOM付き
            writer = csv.writer(csv_output)

            writer.writerow([
                "試験名",
                "受験者名",
                "問題ID",
                "問題文",
                "選択肢",
                "正解",
                "受験者の回答",
                "正誤"
            ])

            for a in ans_list:
                q = question_map.get(a.question_id)
                if not q:
                    continue

                is_correct = (
                    a.selected_answer != "未回答"
                    and a.selected_answer.strip().lower()
                    == q.correct_answer.strip().lower()
                )

                writer.writerow([
                    exam.name,
                    student,
                    q.id,
                    q.question_text,
                    " | ".join(q.choices.split("|")),
                    q.correct_answer,
                    a.selected_answer,
                    "正解" if is_correct else "不正解"
                ])

            # ★ 受験者名.csv（日本語OK）
            zipf.writestr(f"{student}.csv", csv_output.getvalue())

    zip_buffer.seek(0)

    return Response(
        zip_buffer.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Disposition":
                f"attachment; filename=exam_{exam_id}_students.zip"
        }
    )


# ----------------- 起動 -----------------
def create_app():
    with app.app_context():
        db.create_all()
    return app

if __name__ == "__main__":
    create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
