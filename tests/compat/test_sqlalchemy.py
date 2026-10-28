"""sqlite+s3rds vs SQLAlchemy on the standard library's sqlite3: the same ORM / Core code, the same results.

SQLAlchemy writes SQLite SQL for both sides (its own SQLite dialect), so nothing is translated; what is
under test is the driver (DB-API behaviour, types, errors) and the transactions (read your own writes,
commit, rollback, savepoints) over a server that keeps no session. Every test is one feature; a scenario
gets two engines on the same database (`other` plays a second client) and returns plain values.

    pip install pytest boto3 sqlalchemy
    python3 -m pytest tests/compat/test_sqlalchemy.py -v
"""
import datetime
import decimal

import pytest

sa = pytest.importorskip("sqlalchemy")
from sqlalchemy import orm  # noqa: E402


class Base(orm.DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = orm.mapped_column(sa.Integer, primary_key=True)
    name = orm.mapped_column(sa.String(50), unique=True, nullable=False)
    posts = orm.relationship("Post", back_populates="user", cascade="all, delete-orphan", order_by="Post.id")


class Post(Base):
    __tablename__ = "posts"
    id = orm.mapped_column(sa.Integer, primary_key=True)
    user_id = orm.mapped_column(sa.ForeignKey("users.id"), nullable=False, index=True)
    title = orm.mapped_column(sa.String(100), nullable=False)
    user = orm.relationship("User", back_populates="posts")


post_tags = sa.Table("post_tags", Base.metadata,
                     sa.Column("post_id", sa.ForeignKey("posts.id"), primary_key=True),
                     sa.Column("tag_id", sa.ForeignKey("tags.id"), primary_key=True))


class Tag(Base):
    __tablename__ = "tags"
    id = orm.mapped_column(sa.Integer, primary_key=True)
    name = orm.mapped_column(sa.String(30), unique=True, nullable=False)
    posts = orm.relationship("Post", secondary=post_tags, order_by="Post.id")


class Account(Base):
    """Optimistic locking: every UPDATE carries WHERE version = <the version read>."""
    __tablename__ = "accounts"
    id = orm.mapped_column(sa.Integer, primary_key=True)
    balance = orm.mapped_column(sa.Integer, nullable=False)
    version = orm.mapped_column(sa.Integer, nullable=False)
    __mapper_args__ = {"version_id_col": version}


class Employee(Base):
    """Joined-table inheritance: a Manager is one row in employees and one in managers."""
    __tablename__ = "employees"
    id = orm.mapped_column(sa.Integer, primary_key=True)
    name = orm.mapped_column(sa.String(50), nullable=False)
    kind = orm.mapped_column(sa.String(20), nullable=False)
    __mapper_args__ = {"polymorphic_on": kind, "polymorphic_identity": "employee"}


class Manager(Employee):
    __tablename__ = "managers"
    id = orm.mapped_column(sa.ForeignKey("employees.id"), primary_key=True)
    reports = orm.mapped_column(sa.Integer, nullable=False)
    __mapper_args__ = {"polymorphic_identity": "manager"}


class Typed(Base):
    __tablename__ = "typed"
    id = orm.mapped_column(sa.Integer, primary_key=True)
    dt = orm.mapped_column(sa.DateTime)
    d = orm.mapped_column(sa.Date)
    n = orm.mapped_column(sa.Numeric(10, 2))
    f = orm.mapped_column(sa.Float)
    b = orm.mapped_column(sa.Boolean)
    blob = orm.mapped_column(sa.LargeBinary)
    js = orm.mapped_column(sa.JSON)
    e = orm.mapped_column(sa.Enum("draft", "published", name="state"))


def count(session, model):
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def names(engine):
    with orm.Session(engine) as s:
        return s.scalars(sa.select(User.name).order_by(User.id)).all()


# ---- Core ----

def test_core_crud(sqlalchemy_pair):
    """DML | Core: create_all, insert (executemany), update / delete rowcount, select with where / order by / limit"""
    def scenario(engine, other):
        md = sa.MetaData()
        t = sa.Table("t", md, sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("name", sa.String(50), nullable=False),
                     sa.Column("score", sa.Integer, server_default="0"))
        md.create_all(engine)
        with engine.begin() as c:
            c.execute(t.insert(), [{"name": "a", "score": 1}, {"name": "b", "score": 2}])
            c.execute(t.insert(), {"name": "c"})  # server default
            updated = c.execute(t.update().where(t.c.score > 0).values(score=t.c.score + 10)).rowcount
            deleted = c.execute(t.delete().where(t.c.name == "b")).rowcount
        with other.connect() as c:
            rows = c.execute(sa.select(t).order_by(t.c.id).limit(5)).all()
        return updated, deleted, [tuple(r) for r in rows]
    sqlalchemy_pair(scenario)


def test_insert_returning(sqlalchemy_pair):
    """DML | INSERT … RETURNING for one and many rows; ids continue after a delete"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with engine.begin() as c:
            one = c.execute(sa.insert(User).returning(User.id, User.name), {"name": "a"}).all()
            many = c.execute(sa.insert(User).returning(User.id, User.name),
                             [{"name": f"u{i}"} for i in range(5)]).all()
            c.execute(sa.delete(User).where(User.name == "u4"))
            again = c.execute(sa.insert(User).returning(User.id), {"name": "z"}).scalar()
        return [tuple(r) for r in one], sorted(tuple(r) for r in many), again
    sqlalchemy_pair(scenario)


def test_queries(sqlalchemy_pair):
    """Queries | join, group by / having, subquery, distinct, coalesce / lower, offset"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            s.add_all([User(name="Alice", posts=[Post(title="a1"), Post(title="a2")]),
                       User(name="bob", posts=[Post(title="b1")]), User(name="carol")])
            s.commit()
        with orm.Session(other) as s:
            per_user = s.execute(sa.select(User.name, sa.func.count(Post.id))
                                 .outerjoin(Post).group_by(User.id).order_by(User.id)).all()
            busy = s.scalars(sa.select(User.name).join(Post).group_by(User.id)
                             .having(sa.func.count(Post.id) > 1)).all()
            sub = sa.select(Post.user_id).where(Post.title.like("b%")).scalar_subquery()
            in_sub = s.scalars(sa.select(sa.func.lower(User.name)).where(User.id.in_(sub))).all()
            titles = s.scalars(sa.select(Post.title).distinct().order_by(Post.title).offset(1).limit(2)).all()
            missing = s.execute(sa.select(User.name, sa.func.coalesce(Post.title, "-"))
                                .outerjoin(Post).where(Post.id.is_(None))).all()
        return [tuple(r) for r in per_user], busy, in_sub, titles, [tuple(r) for r in missing]
    sqlalchemy_pair(scenario)


# ---- ORM ----

def test_orm_session(sqlalchemy_pair):
    """ORM | Session add / commit / query, autoincrement ids, relationship load, cascade delete-orphan"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            u = User(name="alice", posts=[Post(title="p1"), Post(title="p2")])
            s.add_all([u, User(name="bob")])
            s.commit()
            ids = (u.id, [p.id for p in u.posts])
            u.posts.pop(0)  # delete-orphan
            s.commit()
            s.delete(s.scalars(sa.select(User).where(User.name == "bob")).one())
            s.commit()
        with orm.Session(other) as s:
            users = [(x.id, x.name, [p.title for p in x.posts]) for x in
                     s.scalars(sa.select(User).options(orm.selectinload(User.posts)).order_by(User.id))]
            posts = count(s, Post)
        return ids, users, posts
    sqlalchemy_pair(scenario)


def test_orm_many_to_many(sqlalchemy_pair):
    """ORM | many-to-many through an association table; joinedload; removing a link deletes only the link row"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            py, db = Tag(name="python"), Tag(name="db")
            u = User(name="alice", posts=[Post(title="p1"), Post(title="p2")])
            s.add_all([u, py, db])
            s.flush()
            py.posts.extend(u.posts)
            db.posts.append(u.posts[1])
            s.commit()
            py.posts.remove(u.posts[0])
            s.commit()
        with orm.Session(other) as s:
            tags = [(t.name, [p.title for p in t.posts]) for t in
                    s.scalars(sa.select(Tag).options(orm.joinedload(Tag.posts)).order_by(Tag.id)).unique()]
            links = s.scalar(sa.select(sa.func.count()).select_from(post_tags))
            posts = count(s, Post)
        return tags, links, posts
    sqlalchemy_pair(scenario)


def test_orm_bulk_update_delete_get_merge(sqlalchemy_pair):
    """ORM | bulk update / delete statements with session sync, Session.get, Session.merge"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            s.add_all([User(name=n) for n in ("a", "b", "c", "d")])
            s.commit()
            loaded = s.get(User, 1)
            updated = s.execute(sa.update(User).where(User.id == 1).values(name="a!")
                                .execution_options(synchronize_session="evaluate")).rowcount
            in_session = loaded.name                      # synchronize_session updated the loaded object
            deleted = s.execute(sa.delete(User).where(User.name == "d")).rowcount
            merged = s.merge(User(id=3, name="c-merged"))
            s.commit()
            merged_id = merged.id
        return updated, in_session, deleted, merged_id, names(other)
    sqlalchemy_pair(scenario)


def test_orm_optimistic_locking(sqlalchemy_pair):
    """ORM | version_id_col: the second of two concurrent updates of one row raises StaleDataError"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            s.add(Account(id=1, balance=100))
            s.commit()
        with orm.Session(engine) as first, orm.Session(other) as second:
            a1, a2 = first.get(Account, 1), second.get(Account, 1)   # both read version 1
            a1.balance -= 30
            first.commit()
            a2.balance -= 50
            try:
                second.commit()
                outcome = "committed"
            except orm.exc.StaleDataError:
                outcome = "StaleDataError"
                second.rollback()
        with orm.Session(other) as s:
            row = s.get(Account, 1)
            return outcome, row.balance, row.version
    sqlalchemy_pair(scenario)


def test_orm_inheritance(sqlalchemy_pair):
    """ORM | joined-table inheritance: one object, two tables; polymorphic queries return the subclass"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            s.add_all([Employee(name="eve"), Manager(name="max", reports=3)])
            s.commit()
        with orm.Session(other) as s:
            everyone = [(type(e).__name__, e.name, getattr(e, "reports", None))
                        for e in s.scalars(sa.select(Employee).order_by(Employee.id))]
            managers = s.scalars(sa.select(Manager.name).where(Manager.reports > 1)).all()
            rows = s.execute(sa.text("SELECT COUNT(*) FROM employees")).scalar(), \
                s.execute(sa.text("SELECT COUNT(*) FROM managers")).scalar()
        return everyone, managers, rows
    sqlalchemy_pair(scenario)


# ---- Transactions ----

def test_reads_see_own_writes(sqlalchemy_pair):
    """Transactions | autoflush: a query sees the session's own uncommitted writes; another client sees them only after commit"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s, orm.Session(other) as o:
            s.add(User(name="alice"))
            mine = count(s, User)        # autoflush: the INSERT runs inside the open transaction
            theirs = count(o, User)
            o.rollback()
            s.commit()
            after = count(o, User)
        return mine, theirs, after
    sqlalchemy_pair(scenario)


def test_rollback(sqlalchemy_pair):
    """Transactions | Session.rollback() and an exception inside engine.begin() discard the writes, flushed ones included"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            s.add(User(name="kept"))
            s.commit()
            s.add(User(name="flushed"))
            s.flush()
            s.rollback()
        try:
            with engine.begin() as c:
                c.execute(sa.insert(User), {"name": "in-block"})
                raise RuntimeError("abort")
        except RuntimeError:
            pass
        return names(other)
    sqlalchemy_pair(scenario)


def test_savepoint(sqlalchemy_pair):
    """Transactions | begin_nested (SAVEPOINT): rolling back the inner block keeps the outer writes"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            s.add(User(name="outer"))
            s.flush()
            sp = s.begin_nested()
            s.add(User(name="inner"))
            s.flush()
            sp.rollback()
            s.add(User(name="after"))
            s.commit()
        return names(other)
    sqlalchemy_pair(scenario)


# ---- Constraints & errors ----

def test_integrity_errors(sqlalchemy_pair):
    """Constraints & errors | UNIQUE and FOREIGN KEY violations are sqlalchemy.exc.IntegrityError; the session works again after rollback"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        errors = []
        with orm.Session(engine) as s:
            s.add(User(name="a"))
            s.commit()
            for obj in (User(name="a"), Post(user_id=999, title="orphan")):
                s.add(obj)
                try:
                    s.commit()
                except sa.exc.IntegrityError as e:
                    errors.append(type(e.orig).__name__)
                    s.rollback()
            s.add(User(name="b"))
            s.commit()
        return errors, names(other)
    sqlalchemy_pair(scenario)


# ---- Types ----

def test_types(sqlalchemy_pair):
    """Types | DateTime, Date, Numeric, Float, Boolean, LargeBinary, JSON, Enum round-trip with their Python types"""
    row = dict(dt=datetime.datetime(2026, 10, 27, 13, 14, 15, 123456), d=datetime.date(2026, 1, 2),
               n=decimal.Decimal("12.34"), f=1.5, b=True, blob=b"\x00\x01\xff", js={"k": [1, "x", None]},
               e="published")

    def scenario(engine, other):
        Base.metadata.create_all(engine)
        with orm.Session(engine) as s:
            s.add_all([Typed(**row), Typed()])
            s.commit()
        with orm.Session(other) as s:
            got = s.execute(sa.select(Typed.dt, Typed.d, Typed.n, Typed.f, Typed.b, Typed.blob, Typed.js,
                                      Typed.e).order_by(Typed.id)).all()
        return [[(type(v).__name__, repr(v)) for v in r] for r in got]
    sqlalchemy_pair(scenario)


# ---- Metadata ----

def test_reflection(sqlalchemy_pair):
    """Metadata | inspect(): table names, columns, primary key, foreign keys, indexes, unique constraints"""
    def scenario(engine, other):
        Base.metadata.create_all(engine)
        i = sa.inspect(other)
        return (i.get_table_names(),
                [(c["name"], str(c["type"]), c["nullable"]) for c in i.get_columns("posts")],
                i.get_pk_constraint("posts"), i.get_foreign_keys("posts"),
                i.get_indexes("posts"), i.get_unique_constraints("users"))
    sqlalchemy_pair(scenario)
