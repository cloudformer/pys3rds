"""ENGINE "s3rds.sqlite" vs Django's own SQLite backend: the same ORM code, the same results.

Django writes SQLite SQL for both sides. What is under test is the backend (client/s3rds/sqlite/base.py: the date
functions in SQLite's own SQL instead of Python functions on the connection, the cursor), the driver and its
transactions over a server that keeps no session, and the schema editor (migrations turn foreign keys off for
the connection). Every test is one feature; a scenario gets two database aliases (`other` plays a second client)
and returns plain values. The reference is Django's SQLite backend on the server's pinned SQLite module.

    pip install pytest boto3 "django>=4.2,<6.2" sqlean.py==3.50.4.5     # CI: 4.2, 5.2 and 6.1
    python3 -m pytest tests/compat/test_django.py -v
"""
import datetime
import decimal
import uuid

import pytest

django = pytest.importorskip("django")
from django.core.management import call_command  # noqa: E402
from django.db import IntegrityError, NotSupportedError, connections, migrations, models, transaction  # noqa: E402
from django.db.migrations.state import ProjectState  # noqa: E402
from django.db.models import (Avg, Case, Count, Exists, F, Max, Min, OuterRef, Prefetch, Q, StdDev,  # noqa: E402
                              Subquery, Sum, Value, Variance, When)
from django.db.models import functions as fn  # noqa: E402
from django.utils import timezone  # noqa: E402

UTC = datetime.timezone.utc


class Author(models.Model):
    name = models.CharField(max_length=50, unique=True)
    born = models.DateField(null=True)

    class Meta:
        app_label = "compat"


class Tag(models.Model):
    name = models.CharField(max_length=30, unique=True)

    class Meta:
        app_label = "compat"


class Book(models.Model):
    author = models.ForeignKey(Author, models.CASCADE, related_name="books")
    title = models.CharField(max_length=100)
    pages = models.IntegerField(default=0)
    price = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    published = models.DateTimeField(null=True)
    tags = models.ManyToManyField(Tag, related_name="books")

    class Meta:
        app_label = "compat"


class Place(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "compat"


class Restaurant(Place):
    """Multi-table inheritance: one row in compat_place, one in compat_restaurant."""
    serves_pizza = models.BooleanField(default=False)

    class Meta:
        app_label = "compat"


class Typed(models.Model):
    char = models.CharField(max_length=20, null=True)
    text = models.TextField(null=True)
    integer = models.IntegerField(null=True)
    big = models.BigIntegerField(null=True)
    flt = models.FloatField(null=True)
    dec = models.DecimalField(max_digits=10, decimal_places=3, null=True)
    flag = models.BooleanField(null=True)
    day = models.DateField(null=True)
    moment = models.DateTimeField(null=True)
    clock = models.TimeField(null=True)
    span = models.DurationField(null=True)
    uid = models.UUIDField(null=True)
    blob = models.BinaryField(null=True)
    data = models.JSONField(null=True)

    class Meta:
        app_label = "compat"


MODELS = [Author, Tag, Book, Place, Restaurant, Typed]


def create_tables(db, *only):
    with connections[db].schema_editor() as editor:   # turns foreign keys off for the connection, as migrations do
        for model in only or MODELS:
            editor.create_model(model)


def seed(db):
    create_tables(db)
    alice = Author.objects.using(db).create(name="Alice", born=datetime.date(1970, 5, 17))
    bob = Author.objects.using(db).create(name="bob", born=datetime.date(1985, 11, 3))
    Author.objects.using(db).create(name="carol")
    Book.objects.using(db).bulk_create([
        Book(author=alice, title="Alpha", pages=100, price=decimal.Decimal("9.99"),
             published=datetime.datetime(2024, 1, 15, 9, 30, 5, tzinfo=UTC)),        # a Monday
        Book(author=alice, title="Beta", pages=250, price=decimal.Decimal("19.50"),
             published=datetime.datetime(2024, 5, 3, 18, 0, tzinfo=UTC)),
        Book(author=bob, title="gamma", pages=50, price=None,
             published=datetime.datetime(2025, 12, 31, 23, 59, 59, 123456, tzinfo=UTC)),  # ISO week 1 of 2026
        Book(author=bob, title="Delta 100%", pages=300, price=decimal.Decimal("5.00"), published=None),
    ])


def titles(qs):
    return sorted(qs.values_list("title", flat=True))


# ---- DML / queries ----

def test_crud(django_pair):
    """DML | create / save (insert, then update) / get / QuerySet.update / delete with cascade; auto ids"""
    def scenario(db, other):
        create_tables(db, Author, Tag, Book)
        a = Author.objects.using(db).create(name="alice")
        b = Author(name="bob")
        b.save(using=db)
        b.name = "bobby"
        b.save(using=db)
        Book.objects.using(db).create(author=a, title="x")
        Book.objects.using(db).create(author=b, title="y")
        updated = Author.objects.using(db).filter(name__startswith="b").update(born=datetime.date(2000, 1, 2))
        deleted = Author.objects.using(db).filter(name="alice").delete()
        rows = list(Author.objects.using(other).order_by("id").values_list("id", "name", "born"))
        return a.id, b.id, updated, deleted, rows, titles(Book.objects.using(other))
    django_pair(scenario)


def test_lookups(django_pair):
    """Queries | lookups: exact / iexact / contains (with % in the value) / icontains / startswith / endswith / in / range / isnull / gt / lte, Q, exclude"""
    def scenario(db, other):
        seed(db)
        b = Book.objects.using(other)
        return [titles(b.filter(title__iexact="ALPHA")), titles(b.filter(title__contains="a")),
                titles(b.filter(title__icontains="A")), titles(b.filter(title__contains="100%")),
                titles(b.filter(title__startswith="B")), titles(b.filter(title__endswith="a")),
                titles(b.filter(pages__in=[50, 300])), titles(b.filter(pages__range=(100, 250))),
                titles(b.filter(price__isnull=True)), titles(b.filter(pages__gt=100, pages__lte=300)),
                titles(b.filter(Q(pages__lt=100) | Q(author__name="Alice"))),
                titles(b.exclude(author__name="Alice")), titles(b.filter(~Q(title__icontains="a")))]
    django_pair(scenario)


def test_aggregates_and_subqueries(django_pair):
    """Queries | aggregate / annotate (group by, having), Subquery / OuterRef / Exists, F, Case / When, slicing, distinct, first / last"""
    def scenario(db, other):
        seed(db)
        a, b = Author.objects.using(other), Book.objects.using(other)
        totals = b.aggregate(n=Count("id"), total=Sum("pages"), avg=Avg("pages"), lo=Min("price"), hi=Max("price"))
        per_author = list(a.annotate(n=Count("books"), pages=Sum("books__pages")).order_by("id")
                          .values_list("name", "n", "pages"))
        busy = list(a.annotate(n=Count("books")).filter(n__gt=1).order_by("id").values_list("name", flat=True))
        longest = list(a.annotate(t=Subquery(b.filter(author=OuterRef("pk")).order_by("-pages").values("title")[:1]))
                       .order_by("id").values_list("name", "t"))
        writers = list(a.filter(Exists(b.filter(author=OuterRef("pk")))).order_by("id").values_list("name", flat=True))
        size = list(b.annotate(size=Case(When(pages__gte=250, then=Value("long")), default=Value("short")))
                    .order_by("id").values_list("title", "size"))
        page = list(b.order_by("-pages", "title")[1:3].values_list("title", flat=True))
        names = list(b.values_list("author__name", flat=True).distinct().order_by("author__name"))
        more = titles(b.filter(pages__gt=F("author__books__pages")).distinct())
        return (totals, per_author, busy, longest, writers, size, page, names, more,
                b.order_by("pages").first().title, b.order_by("pages").last().title,
                b.filter(pages__gt=1000).exists(), b.count())
    django_pair(scenario)


# ---- ORM ----

def test_foreign_keys(django_pair):
    """ORM | ForeignKey: select_related, prefetch_related, reverse relation, filter across the relation, cascade delete"""
    def scenario(db, other):
        seed(db)
        a, b = Author.objects.using(other), Book.objects.using(other)
        joined = [(x.title, x.author.name) for x in b.select_related("author").order_by("id")]
        prefetched = [(x.name, [y.title for y in x.books.all()]) for x in
                      a.prefetch_related(Prefetch("books", queryset=Book.objects.order_by("id"))).order_by("id")]
        across = titles(b.filter(author__name__startswith="A"))
        reverse = list(a.get(name="bob").books.order_by("id").values_list("title", flat=True))
        a.get(name="Alice").delete()
        return joined, prefetched, across, reverse, b.count()
    django_pair(scenario)


def test_many_to_many(django_pair):
    """ORM | ManyToMany: add / remove / set / clear, filter and annotate through the join table"""
    def scenario(db, other):
        seed(db)
        b, t = Book.objects.using(db), Tag.objects.using(db)
        sql, orm = t.create(name="sql"), t.create(name="orm")
        alpha, beta, gamma = b.get(title="Alpha"), b.get(title="Beta"), b.get(title="gamma")
        alpha.tags.add(sql, orm)
        beta.tags.add(sql)
        beta.tags.remove(sql)
        beta.tags.set([orm])
        gamma.tags.add(sql)
        gamma.tags.clear()
        tagged = titles(Book.objects.using(other).filter(tags__name="orm"))
        counts = list(Tag.objects.using(other).annotate(n=Count("books")).order_by("name").values_list("name", "n"))
        return tagged, counts
    django_pair(scenario)


def test_bulk_and_upsert(django_pair):
    """ORM | bulk_create (ids returned), bulk_update, get_or_create, update_or_create, in_bulk, F() update, bulk_create(update_conflicts=True)"""
    def scenario(db, other):
        create_tables(db, Author, Tag)
        made = Tag.objects.using(db).bulk_create([Tag(name=f"t{i}") for i in range(5)])
        for tag in made:
            tag.name = tag.name.upper()
        updated = Tag.objects.using(db).bulk_update(made, ["name"])
        got = [Tag.objects.using(db).get_or_create(name=n)[1] for n in ("T1", "new")]
        upserted = [Author.objects.using(db).update_or_create(name="ann", defaults={"born": d})[1]
                    for d in (datetime.date(1999, 9, 9), datetime.date(2001, 1, 1))]
        Tag.objects.using(db).bulk_create([Tag(name="T0"), Tag(name="z")], update_conflicts=True,
                                          unique_fields=["name"], update_fields=["name"])
        Author.objects.using(db).filter(name="ann").update(name=fn.Concat(F("name"), Value("!")))
        in_bulk = {k: v.name for k, v in Tag.objects.using(other).in_bulk([made[0].id, made[1].id]).items()}
        return ([x.id for x in made], updated, got, upserted, in_bulk,
                list(Tag.objects.using(other).order_by("id").values_list("id", "name")),
                list(Author.objects.using(other).values_list("name", "born")))
    django_pair(scenario)


def test_inheritance(django_pair):
    """ORM | multi-table inheritance: a Restaurant is one Place row and one Restaurant row, saved atomically"""
    def scenario(db, other):
        create_tables(db, Place, Restaurant)
        Place.objects.using(db).create(name="park")
        r = Restaurant.objects.using(db).create(name="luigi", serves_pizza=True)
        r.name = "Luigi's"
        r.save(using=db)
        return (list(Place.objects.using(other).order_by("id").values_list("id", "name")),
                [(x.id, x.name, x.serves_pizza) for x in Restaurant.objects.using(other).all()],
                Place.objects.using(other).get(name="Luigi's").restaurant.serves_pizza)
    django_pair(scenario)


# ---- transactions ----

def test_atomic_visibility(django_pair):
    """Transactions | atomic(): reads inside see the block's own writes, another connection only after commit"""
    def scenario(db, other):
        create_tables(db, Tag)
        seen = []
        with transaction.atomic(using=db):
            Tag.objects.using(db).create(name="a")
            seen += [Tag.objects.using(db).count(), Tag.objects.using(other).count()]
        seen.append(Tag.objects.using(other).count())
        return seen
    django_pair(scenario)


def test_atomic_rollback_and_savepoints(django_pair):
    """Transactions | an exception rolls the atomic block back; a nested atomic (savepoint) rolls back alone; on_commit"""
    def scenario(db, other):
        create_tables(db, Tag)
        try:
            with transaction.atomic(using=db):
                Tag.objects.using(db).create(name="gone")
                raise RuntimeError
        except RuntimeError:
            pass
        committed = []
        with transaction.atomic(using=db):
            Tag.objects.using(db).create(name="kept")
            try:
                with transaction.atomic(using=db):
                    Tag.objects.using(db).create(name="inner")
                    raise RuntimeError
            except RuntimeError:
                pass
            with transaction.atomic(using=db):
                Tag.objects.using(db).create(name="inner2")
            transaction.on_commit(lambda: committed.append("ran"), using=db)
        return sorted(Tag.objects.using(other).values_list("name", flat=True)), committed
    django_pair(scenario)


# ---- constraints, types ----

def test_integrity_errors(django_pair):
    """Constraints & errors | unique and foreign-key violations raise IntegrityError (in atomic and in autocommit); nothing lands"""
    def scenario(db, other):
        create_tables(db, Author, Book, Tag)
        Tag.objects.using(db).create(name="a")
        errors = []
        for action in (lambda: Tag.objects.using(db).create(name="a"),
                       lambda: Book.objects.using(db).create(author_id=999, title="orphan")):
            for atomic in (True, False):
                try:
                    if atomic:
                        with transaction.atomic(using=db):
                            action()
                    else:
                        action()
                except IntegrityError as e:
                    errors.append(type(e).__name__)
        return errors, Tag.objects.using(other).count(), Book.objects.using(other).count()
    django_pair(scenario)


def test_types(django_pair):
    """Types | every common field type round-trips: aware datetime, Decimal, Duration, UUID, bytes, JSON (with key lookups), bool, NULL"""
    def scenario(db, other):
        create_tables(db, Typed)
        values = dict(char="ü€", text="x" * 1000, integer=-5, big=2 ** 62, flt=1.5, dec=decimal.Decimal("12.345"),
                      flag=True, day=datetime.date(2024, 2, 29),
                      moment=datetime.datetime(2024, 2, 29, 23, 59, 59, 999999, tzinfo=UTC),
                      clock=datetime.time(7, 8, 9, 10), span=datetime.timedelta(days=2, microseconds=5),
                      uid=uuid.UUID("12345678-1234-5678-1234-567812345678"), blob=b"\x00\xff",
                      data={"a": [1, 2, {"b": None}], "c": "d"})
        full = Typed.objects.using(db).create(**values)
        empty = Typed.objects.using(db).create()
        got = Typed.objects.using(other).get(pk=full.pk)
        out = {k: getattr(got, k) for k in values}
        out["blob"] = bytes(out["blob"])
        nulls = [getattr(Typed.objects.using(other).get(pk=empty.pk), k) for k in values]
        q = Typed.objects.using(other)
        lookups = [list(q.filter(data__c="d").values_list("pk", flat=True)),
                   list(q.filter(data__a__1=2).values_list("pk", flat=True)),
                   list(q.filter(data__has_key="a").values_list("pk", flat=True)),
                   list(q.filter(flag=True, moment__gte=values["moment"], span__gt=datetime.timedelta(days=1))
                        .values_list("pk", flat=True))]
        return {k: (type(v).__name__, v) for k, v in out.items()}, nulls, lookups
    django_pair(scenario)


# ---- functions: the date ones are SQLite's strftime() on s3rds, Python functions in the reference ----

def test_date_lookups(django_pair):
    """Functions | date lookups: __year / __month / __day / __week_day / __iso_week_day / __week / __iso_year / __quarter / __hour / __minute / __second / __date / __time"""
    def scenario(db, other):
        seed(db)
        b = Book.objects.using(other)
        return [titles(b.filter(published__year=2024)), titles(b.filter(published__year__gte=2025)),
                titles(b.filter(published__month=5)), titles(b.filter(published__day=31)),
                titles(b.filter(published__week_day=2)), titles(b.filter(published__iso_week_day=5)),
                titles(b.filter(published__week=1)), titles(b.filter(published__iso_year=2026)),
                titles(b.filter(published__quarter=4)), titles(b.filter(published__hour=18)),
                titles(b.filter(published__minute=30)), titles(b.filter(published__second=59)),
                titles(b.filter(published__date=datetime.date(2024, 5, 3))),
                titles(b.filter(published__date__gt=datetime.date(2024, 6, 1))),
                titles(b.filter(published__time=datetime.time(9, 30, 5))),
                titles(b.filter(published__time=datetime.time(23, 59, 59, 123456))),
                sorted(Author.objects.using(other).filter(born__year=1970, born__month=5, born__week_day=1)
                       .values_list("name", flat=True))]
    django_pair(scenario)


def test_trunc_and_extract(django_pair):
    """Functions | Trunc (year … second, TruncDate / TruncTime) and Extract annotations, group by month, QuerySet.dates() / datetimes()"""
    def scenario(db, other):
        seed(db)
        b = Book.objects.using(other).exclude(published=None).order_by("id")
        trunc = [list(b.annotate(x=fn.Trunc("published", kind)).values_list("x", flat=True))
                 for kind in ("year", "quarter", "month", "week", "day", "hour", "minute", "second")]
        cast = list(b.annotate(d=fn.TruncDate("published"), t=fn.TruncTime("published")).values_list("d", "t"))
        extract = [list(b.annotate(x=fn.Extract("published", kind)).values_list("x", flat=True))
                   for kind in ("year", "quarter", "month", "week", "week_day", "iso_week_day", "iso_year",
                                "day", "hour", "minute", "second")]
        per_month = list(b.annotate(m=fn.TruncMonth("published")).values("m").annotate(n=Count("id"))
                         .order_by("m").values_list("m", "n"))
        born = list(Author.objects.using(other).exclude(born=None).order_by("id")
                    .annotate(q=fn.TruncQuarter("born"), w=fn.TruncWeek("born"), d=fn.ExtractWeekDay("born"))
                    .values_list("q", "w", "d"))
        return (trunc, cast, extract, per_month, born, list(Book.objects.using(other).dates("published", "month")),
                list(Book.objects.using(other).datetimes("published", "hour")))
    django_pair(scenario)


def test_builtin_functions(django_pair):
    """Functions | text and math functions SQLite has built in: Lower / Upper / Length / Concat / Substr / Replace / StrIndex / Left / Right / Trim, Abs / Round / Ceil / Floor / Mod / Power (**) / Sqrt / Sign, Coalesce / Greatest / Least / Cast"""
    def scenario(db, other):
        seed(db)
        b = Book.objects.using(other).order_by("id")
        ratio = fn.Cast("pages", models.FloatField()) / Value(7.0)
        text = list(b.annotate(
            a=fn.Lower("title"), b=fn.Upper("title"), c=fn.Length("title"), d=fn.Concat("title", Value("/"), "author__name"),
            e=fn.Substr("title", 2, 3), f=fn.Replace("title", Value("a"), Value("@")), g=fn.StrIndex("title", Value("a")),
            h=fn.Left("title", 2), i=fn.Right("title", 2), j=fn.Trim(Value("  x  "))).values_list(*"abcdefghij"))
        math = list(b.annotate(
            a=fn.Abs(F("pages") - 200), b=fn.Round(ratio, 2), c=fn.Ceil(ratio), d=fn.Floor(ratio), e=fn.Mod("pages", 7),
            f=fn.Power("pages", 2), g=F("pages") ** 2, h=fn.Sqrt("pages"), i=fn.Sign(F("pages") - 100),
            j=fn.Coalesce("price", Value(decimal.Decimal("0"))), k=fn.Greatest("pages", Value(120)),
            l=fn.Least("pages", Value(120)), m=fn.Cast("pages", models.CharField())).values_list(*"abcdefghijklm"))
        return text, math
    django_pair(scenario)


# ---- metadata, migrations ----

def test_schema_editor(django_pair):
    """Metadata | migration operations: create, add / alter (table rebuild, foreign keys off) / rename / remove a field, unique constraint, rename a model; introspection"""
    def scenario(db, other):
        ops = [
            migrations.CreateModel("Item", [("id", models.AutoField(primary_key=True)),
                                            ("name", models.CharField(max_length=20))]),
            migrations.CreateModel("Part", [("id", models.AutoField(primary_key=True)),
                                            ("item", models.ForeignKey("schema.Item", models.CASCADE)),
                                            ("label", models.CharField(max_length=20))]),
            "rows",
            migrations.AddField("Item", "rank", models.IntegerField(default=7)),
            migrations.AlterField("Item", "name", models.CharField(max_length=80, null=True, db_index=True)),
            migrations.RenameField("Item", "rank", "score"),
            migrations.RemoveField("Part", "label"),
            migrations.AddConstraint("Item", models.UniqueConstraint(fields=["name", "score"], name="uniq_name_score")),
            migrations.RenameModel("Item", "Thing"),
        ]
        state = ProjectState()
        for op in ops:
            if isinstance(op, str):  # "rows": data that every later rebuild must carry over, with a foreign key pointing at it
                with connections[db].cursor() as c:
                    c.execute('INSERT INTO schema_item (name) VALUES (%s)', ["kept"])
                    c.execute('INSERT INTO schema_part (item_id, label) VALUES (%s, %s)', [1, "p"])
                continue
            new = state.clone()
            op.state_forwards("schema", new)
            with connections[db].schema_editor() as editor:
                op.database_forwards("schema", editor, state, new)
            state = new
        conn = connections[other]
        with conn.cursor() as c:
            tables = sorted(t.name for t in conn.introspection.get_table_list(c) if t.name.startswith("schema_"))
            columns = {t: [tuple(d) for d in conn.introspection.get_table_description(c, t)] for t in tables}
            constraints = {t: sorted((k, v["columns"], v["unique"], v["primary_key"], v["index"], v["foreign_key"])
                                     for k, v in conn.introspection.get_constraints(c, t).items()) for t in tables}
            rows = {t: c.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
        return tables, columns, constraints, rows
    django_pair(scenario)


def test_migrate(django_pair):
    """Metadata | migrate: the django.contrib.auth and contenttypes migrations, create_user / check_password, permissions"""
    def scenario(db, other):
        from django.contrib.auth.models import Permission, User
        call_command("migrate", database=db, verbosity=0, interactive=False)
        User.objects.db_manager(db).create_user("ann", "ann@example.com", "s3cret")
        user = User.objects.using(other).get(username="ann")
        with connections[other].cursor() as c:
            tables = sorted(connections[other].introspection.table_names(c))
        return tables, user.check_password("s3cret"), user.check_password("x"), Permission.objects.using(other).count()
    django_pair(scenario)


# ---- unsupported by design: Django's SQLite backend runs these as Python functions on the connection;
#      s3rds runs no Python on the server, so they raise NotSupportedError (move to RDS for them) ----

@pytest.mark.unsupported("needs Python (zoneinfo) on the server; queries in the connection's TIME_ZONE work")
def test_unsupported_timezone_conversion(django_pair):
    """Unsupported | date functions converted to another time zone (USE_TZ, a current time zone other than the connection's)"""
    def scenario(db, other):
        seed(db)
        with timezone.override("Asia/Shanghai"):
            return titles(Book.objects.using(db).filter(published__date=datetime.date(2024, 1, 15)))
    django_pair.unsupported(scenario)


@pytest.mark.unsupported("REGEXP is a Python function in Django's SQLite backend")
def test_unsupported_regex(django_pair):
    """Unsupported | __regex / __iregex lookups"""
    def scenario(db, other):
        seed(db)
        return titles(Book.objects.using(db).filter(title__regex=r"^[AB]"))
    django_pair.unsupported(scenario)
    with pytest.raises(NotSupportedError):
        titles(Book.objects.using("s3").filter(title__iregex=r"^a"))


@pytest.mark.unsupported("Python functions in Django's SQLite backend")
def test_unsupported_python_functions(django_pair):
    """Unsupported | MD5 / SHA1 … SHA512, LPad / RPad / Repeat / Reverse, Cot, order_by("?") (Random)"""
    funcs = [fn.MD5("title"), fn.SHA1("title"), fn.SHA224("title"), fn.SHA256("title"), fn.SHA384("title"),
             fn.SHA512("title"), fn.LPad("title", 8, Value("*")), fn.RPad("title", 8, Value("*")),
             fn.Repeat("title", 2), fn.Reverse("title"), fn.Cot("pages")]

    def scenario(db, other):
        seed(db)
        return list(Book.objects.using(db).annotate(x=funcs[0]).values_list("x", flat=True))
    django_pair.unsupported(scenario)
    for f in funcs[1:]:
        with pytest.raises(NotSupportedError):
            list(Book.objects.using("s3").annotate(x=f).values_list("x", flat=True))
    with pytest.raises(NotSupportedError):
        list(Book.objects.using("s3").order_by("?"))


@pytest.mark.unsupported("Python aggregates in Django's SQLite backend")
def test_unsupported_statistics(django_pair):
    """Unsupported | StdDev / Variance aggregates"""
    def scenario(db, other):
        seed(db)
        return Book.objects.using(db).aggregate(s=StdDev("pages"))
    django_pair.unsupported(scenario)
    with pytest.raises(NotSupportedError):
        Book.objects.using("s3").aggregate(v=Variance("pages", sample=True))


@pytest.mark.unsupported("django_format_dtdelta / django_timestamp_diff are Python functions in Django's SQLite backend")
def test_unsupported_duration_arithmetic(django_pair):
    """Unsupported | date / time arithmetic in the query: F("dt") + timedelta, F("end") - F("start")"""
    def scenario(db, other):
        seed(db)
        return list(Book.objects.using(db).annotate(due=F("published") + datetime.timedelta(days=30))
                    .values_list("due", flat=True))
    django_pair.unsupported(scenario)
    with pytest.raises(NotSupportedError):
        list(Book.objects.using("s3").annotate(age=F("published") - F("published")).values_list("age", flat=True))
