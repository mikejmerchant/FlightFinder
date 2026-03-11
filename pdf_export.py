"""
pdf_export.py — Beautiful PDF report generator for Flight Finder tools.
Used by flight_finder.py, FlightFinderAdvanced.py and FlightFinderFriends.py.

Requires: pip install reportlab
"""

from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, KeepTogether,
)
from reportlab.platypus import Flowable

# ── Colour palette ─────────────────────────────────────────────────────────────
NAVY      = colors.HexColor('#0B2545')
TEAL      = colors.HexColor('#1B7A8C')
ACCENT    = colors.HexColor('#E8A020')
LIGHT_BG  = colors.HexColor('#F4F7FB')
MID_GREY  = colors.HexColor('#8A9BB0')
DARK_GREY = colors.HexColor('#2E3D4F')
WHITE     = colors.white
GREEN     = colors.HexColor('#2E7D32')
ORANGE    = colors.HexColor('#E67E22')
RED_SOFT  = colors.HexColor('#C0392B')

# Per-traveller accent colours (Friends mode)
TRAVELLER_PALETTE = [
    colors.HexColor('#1B7A8C'),
    colors.HexColor('#7B3FA0'),
    colors.HexColor('#C0392B'),
    colors.HexColor('#E67E22'),
    colors.HexColor('#27AE60'),
    colors.HexColor('#2980B9'),
]

PAGE_W, PAGE_H = A4
L_MARGIN = R_MARGIN = 14 * mm
CONTENT_W = PAGE_W - L_MARGIN - R_MARGIN


# ── Styles ──────────────────────────────────────────────────────────────────────
def _styles():
    return {
        'section_head': ParagraphStyle('sh',
            fontName='Helvetica-Bold', fontSize=11, textColor=NAVY,
            spaceBefore=8, spaceAfter=3),
        'summary': ParagraphStyle('sm',
            fontName='Helvetica', fontSize=10, textColor=DARK_GREY,
            leading=15, spaceAfter=4),
        'footer': ParagraphStyle('ft',
            fontName='Helvetica', fontSize=7, textColor=MID_GREY,
            alignment=TA_CENTER),
        'trip_total': ParagraphStyle('tt',
            fontName='Helvetica-Bold', fontSize=13, textColor=GREEN),
        'sync_label': ParagraphStyle('sl',
            fontName='Helvetica', fontSize=9, textColor=MID_GREY),
    }


# ── Custom Flowables ────────────────────────────────────────────────────────────

class HeaderBanner(Flowable):
    """Full-width navy banner with title, subtitle and search query."""

    def __init__(self, title, subtitle, query, width):
        super().__init__()
        self._title    = title
        self._subtitle = subtitle
        self._query    = query
        self._w        = width
        self._h        = 100         # taller banner for breathing room

    def wrap(self, aw, ah):
        return self._w, self._h

    def _split_query(self, query: str, max_line: int = 90) -> tuple[str, str]:
        """Word-wrap query onto two lines."""
        if len(query) <= max_line:
            return query, ''
        cut = query.rfind(' ', 0, max_line)
        if cut == -1:
            cut = max_line
        line1 = query[:cut]
        line2 = query[cut:].strip()
        if len(line2) > max_line + 10:
            line2 = line2[:max_line + 9] + '…'
        return line1, line2

    def draw(self):
        c = self.canv
        h = self._h
        # Navy background
        c.setFillColor(NAVY)
        c.rect(0, 0, self._w, h, fill=1, stroke=0)
        # Amber top strip
        c.setFillColor(ACCENT)
        c.rect(0, h - 5, self._w, 5, fill=1, stroke=0)
        # Title
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 20)
        c.drawString(14*mm, h - 30, self._title)
        # Subtitle
        c.setFillColor(colors.HexColor('#B8D0E8'))
        c.setFont('Helvetica', 8)
        c.drawString(14*mm, h - 44, self._subtitle)
        # Query pill — tall enough for two lines
        line1, line2 = self._split_query(self._query)
        pill_h = 42 if line2 else 26
        pill_y = 8
        c.setFillColor(colors.HexColor('#142240'))
        c.roundRect(14*mm, pill_y, self._w - 28*mm, pill_h, 4, fill=1, stroke=0)
        # "YOUR SEARCH:" label
        c.setFillColor(colors.HexColor('#7A9CC4'))
        c.setFont('Helvetica-Bold', 7)
        c.drawString(18*mm, pill_y + pill_h - 11, 'YOUR SEARCH:')
        # Query text — line 1
        c.setFillColor(WHITE)
        c.setFont('Helvetica', 9)
        c.drawString(18*mm, pill_y + pill_h - 23, line1)
        # Query text — line 2 (if needed)
        if line2:
            c.drawString(18*mm, pill_y + pill_h - 35, line2)


class FlightCard(Flowable):
    """
    One flight result card.  Works for single flights, and as a sub-card
    within trip pairings (pass leg_label e.g. 'OUTBOUND', 'RETURN').
    Pass traveller_name and traveller_color for Friends mode.
    """

    CARD_H_BASE = 80
    CARD_H_BIKE = 93

    def __init__(self, rank, f: dict, width,
                 accent=None, leg_label=None,
                 traveller_name=None, traveller_color=None,
                 show_rank=True, bike_fee=None):
        super().__init__()
        self.rank            = rank
        self.f               = f
        self._w              = width
        self.accent          = accent or TEAL
        self.leg_label       = leg_label
        self.traveller_name  = traveller_name
        self.traveller_color = traveller_color or accent or TEAL
        self.show_rank       = show_rank
        self.bike_fee        = bike_fee
        self.CARD_H          = self.CARD_H_BIKE if bike_fee else self.CARD_H_BASE

    def wrap(self, aw, ah):
        return self._w, self.CARD_H + 6

    def draw(self):
        c  = self.canv
        f  = self.f
        h  = self.CARD_H
        lx = 10 * mm          # left content x

        # Card background
        c.setFillColor(LIGHT_BG)
        c.roundRect(0, 3, self._w, h, 4, fill=1, stroke=0)
        # Left accent strip
        c.setFillColor(self.accent)
        c.roundRect(0, 3, 4, h, 2, fill=1, stroke=0)

        # ── Top-left label: rank number OR leg label ──────────────────────────
        label_x = lx
        if self.leg_label:
            c.setFillColor(self.accent)
            c.roundRect(label_x - 2, h - 15, 58, 13, 2, fill=1, stroke=0)
            c.setFillColor(WHITE)
            c.setFont('Helvetica-Bold', 7)
            c.drawString(label_x + 1, h - 8, self.leg_label.upper())
            name_x = label_x + 64
        elif self.show_rank:
            c.setFillColor(self.accent)
            c.setFont('Helvetica-Bold', 18)
            c.drawString(label_x, h - 20, f'#{self.rank}')
            name_x = label_x + 28
        else:
            name_x = label_x

        # Traveller name badge (Friends mode)
        if self.traveller_name:
            c.setFillColor(self.traveller_color)
            c.roundRect(name_x - 2, h - 15, len(self.traveller_name)*5 + 14, 13, 2,
                        fill=1, stroke=0)
            c.setFillColor(WHITE)
            c.setFont('Helvetica-Bold', 8)
            c.drawString(name_x + 2, h - 8, self.traveller_name)
            airline_x = name_x + len(self.traveller_name)*5 + 22
        else:
            airline_x = name_x

        # Airline name
        c.setFillColor(DARK_GREY)
        c.setFont('Helvetica-Bold', 11)
        c.drawString(airline_x, h - 12, f.get('airline', 'Unknown Airline'))

        # ── Route: ORIGIN → DESTINATION ──────────────────────────────────────
        ry = h - 35
        c.setFillColor(NAVY)
        c.setFont('Helvetica-Bold', 16)
        c.drawString(lx, ry, f.get('origin', ''))
        c.setFillColor(MID_GREY)
        c.setFont('Helvetica', 13)
        c.drawString(lx + 28, ry, '→')
        c.setFillColor(NAVY)
        c.setFont('Helvetica-Bold', 16)
        c.drawString(lx + 46, ry, f.get('destination', ''))
        # Date
        date_str = f.get('date', '')
        try:
            date_str = datetime.strptime(date_str, '%Y-%m-%d').strftime('%a %d %b %Y')
        except Exception:
            pass
        c.setFillColor(MID_GREY)
        c.setFont('Helvetica', 8)
        c.drawString(lx + 84, ry + 2, date_str)

        # ── Times / duration row ──────────────────────────────────────────────
        ty = ry - 15
        depart = f.get('depart', '--:--')
        arrive = f.get('arrive', '--:--')
        c.setFillColor(DARK_GREY)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(lx, ty, depart)
        c.setFillColor(MID_GREY)
        c.setFont('Helvetica', 9)
        c.drawString(lx + 38, ty, '→')
        c.setFillColor(DARK_GREY)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(lx + 52, ty, arrive)
        tt    = f.get('travel_time', '')
        stops = str(f.get('stops', '0'))
        stops_str = 'Direct' if stops == '0' else f'{stops} stop(s)'
        detail = f'  ·  {tt}  ·  {stops_str}' if tt else f'  ·  {stops_str}'
        c.setFillColor(MID_GREY)
        c.setFont('Helvetica', 8)
        c.drawString(lx + 76, ty, detail)

        # ── Bike fee row (if available) ───────────────────────────────────────
        bf = self.bike_fee
        if bf is not None:
            bike_y = 22
            # Bike icon + fee
            if bf.fee_gbp is not None:
                total = f.get('price_gbp', 0) + bf.fee_gbp
                bike_txt = (f"🚲  +  £{bf.fee_gbp:,.0f} bike fee  =  £{total:,.0f} with bike"
                            f"  ({bf.confidence} confidence)")
                bike_color = GREEN if bf.confidence == 'high' else ORANGE
            elif bf.error:
                bike_txt = f"🚲  Bike fee: {bf.error[:70]}"
                bike_color = ORANGE
            else:
                bike_txt = "🚲  Bike fee: check airline website"
                bike_color = MID_GREY
            c.setFillColor(bike_color)
            c.setFont('Helvetica-Bold', 7.5)
            c.drawString(lx, bike_y, bike_txt)
            # Notes (if any)
            if bf.notes:
                c.setFillColor(MID_GREY)
                c.setFont('Helvetica', 7)
                note = bf.notes[:95] + '…' if len(bf.notes) > 97 else bf.notes
                c.drawString(lx, bike_y - 9, note)

        # ── Booking URL ───────────────────────────────────────────────────────
        url = f.get('url', '')
        if url:
            c.setFillColor(TEAL)
            c.setFont('Helvetica', 7)
            disp = url[:96] + '…' if len(url) > 98 else url
            c.drawString(lx, 10, disp)

        # ── Price badge (right side) ──────────────────────────────────────────
        px    = self._w - 30*mm
        price = f.get('price_gbp', 0)
        c.setFillColor(WHITE)
        c.roundRect(px - 3, ry - 20, 28*mm, 46, 3, fill=1, stroke=0)
        c.setFillColor(ACCENT)
        c.setFont('Helvetica-Bold', 18)
        c.drawCentredString(px + 11*mm, ry + 4, f'£{price:,.0f}')
        c.setFillColor(MID_GREY)
        c.setFont('Helvetica', 7)
        c.drawCentredString(px + 11*mm, ry - 8, 'per person')
        badge = GREEN if stops == '0' else ORANGE
        c.setFillColor(badge)
        c.roundRect(px + 1, ry - 19, 25*mm, 10, 2, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 7)
        c.drawCentredString(px + 13*mm, ry - 13, stops_str.upper())


class TripDivider(Flowable):
    """A thin separator bar between outbound and return sections inside a trip."""

    def __init__(self, label, total_price_str, width, sync_info=''):
        super().__init__()
        self._label     = label
        self._total     = total_price_str
        self._w         = width
        self._sync      = sync_info
        self._h         = 18

    def wrap(self, aw, ah):
        return self._w, self._h

    def draw(self):
        c = self.canv
        c.setFillColor(NAVY)
        c.roundRect(0, 2, self._w, self._h - 2, 3, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(6*mm, 8, self._label)
        if self._total:
            c.setFillColor(ACCENT)
            c.setFont('Helvetica-Bold', 10)
            c.drawRightString(self._w - 6*mm, 7, self._total)
        if self._sync:
            c.setFillColor(colors.HexColor('#7A9CC4'))
            c.setFont('Helvetica', 7)
            c.drawString(60*mm, 8, self._sync)


class SyncBadge(Flowable):
    """Displays arrival/departure sync info for a GroupTrip."""

    def __init__(self, arrival_mins, departure_mins, width):
        super().__init__()
        self._arr = arrival_mins
        self._dep = departure_mins
        self._w   = width
        self._h   = 16

    def wrap(self, aw, ah):
        return self._w, self._h

    def _label(self, mins):
        if mins < 0:  return ('unknown', MID_GREY)
        if mins == 0: return ('Same time ✓', GREEN)
        h, m = divmod(mins, 60)
        txt = f'{h}h {m}m gap' if h and m else (f'{h}h gap' if h else f'{m}m gap')
        color = GREEN if mins <= 30 else (ORANGE if mins <= 90 else RED_SOFT)
        return txt, color

    def draw(self):
        c = self.canv
        c.setFillColor(colors.HexColor('#EDF2F7'))
        c.roundRect(0, 0, self._w, self._h, 3, fill=1, stroke=0)
        arr_txt, arr_col = self._label(self._arr)
        dep_txt, dep_col = self._label(self._dep)
        c.setFillColor(MID_GREY)
        c.setFont('Helvetica-Bold', 7)
        c.drawString(4*mm, 6, 'ARRIVAL GAP:')
        c.setFillColor(arr_col)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(28*mm, 5, arr_txt)
        c.setFillColor(MID_GREY)
        c.setFont('Helvetica-Bold', 7)
        c.drawString(65*mm, 6, 'DEPARTURE GAP:')
        c.setFillColor(dep_col)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(93*mm, 5, dep_txt)


# ── Public API ──────────────────────────────────────────────────────────────────

def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(MID_GREY)
    canvas.setFont('Helvetica', 7)
    canvas.drawCentredString(
        PAGE_W / 2, 8*mm,
        f'AI Flight Finder  ·  Live prices from Google Flights  ·  '
        f'{datetime.now().strftime("%d %b %Y %H:%M")}  ·  Page {doc.page}'
    )
    canvas.restoreState()


def _section(label, story, S):
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(label, S['section_head']))
    story.append(HRFlowable(width=CONTENT_W, thickness=1,
                             color=TEAL, spaceAfter=4))


def _flight_to_dict(f) -> dict:
    """Convert a FlightResult dataclass to the plain dict the card expects."""
    return {
        'airline':    getattr(f, 'airline',    'Unknown'),
        'origin':     getattr(f, 'origin',     ''),
        'destination':getattr(f, 'destination',''),
        'date':       getattr(f, 'depart_date',''),
        'depart':     getattr(f, 'depart_time',''),
        'arrive':     getattr(f, 'arrive_time',''),
        'travel_time':getattr(f, 'total_travel_time', '') or getattr(f, 'duration', ''),
        'stops':      getattr(f, 'stops',      '0'),
        'price_gbp':  getattr(f, 'price_val',  0.0),
        'url':        f.booking_url() if hasattr(f, 'booking_url') else '',
        'traveller':  getattr(f, 'traveller',  ''),
        '_bike_fee':  getattr(f, 'bike_fee',   None),   # BikeFee or None
    }


def export_simple(query: str, results: list, summary: str,
                  filename: str = 'flight_results.pdf') -> str:
    """
    Export results from flight_finder.py (simple one-way / return search).
    `results` is a list of FlightResult objects.
    Returns the output filename.
    """
    S = _styles()
    doc = SimpleDocTemplate(
        filename, pagesize=A4,
        leftMargin=L_MARGIN, rightMargin=R_MARGIN,
        topMargin=10*mm, bottomMargin=16*mm,
    )
    story = []

    story.append(HeaderBanner(
        '✈  Flight Finder Results',
        f'AI Flight Finder  ·  Live Google Flights prices  ·  '
        f'{datetime.now().strftime("%d %b %Y %H:%M")}',
        query, CONTENT_W,
    ))
    story.append(Spacer(1, 5*mm))

    if summary:
        _section('AI Travel Summary', story, S)
        story.append(Paragraph(summary, S['summary']))
        story.append(Spacer(1, 3*mm))

    _section(f'Top {min(len(results), 20)} Flights', story, S)
    for i, f in enumerate(results[:20], 1):
        fd = _flight_to_dict(f)
        story.append(KeepTogether(FlightCard(i, fd, CONTENT_W,
                                             bike_fee=fd.get('_bike_fee'))))
        story.append(Spacer(1, 2*mm))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return filename


def export_advanced(query: str, trips: list, summary: str,
                    filename: str = 'flight_results_advanced.pdf') -> str:
    """
    Export results from FlightFinderAdvanced.py (open-jaw trips).
    `trips` is a list of TripResult objects.
    Returns the output filename.
    """
    S = _styles()
    doc = SimpleDocTemplate(
        filename, pagesize=A4,
        leftMargin=L_MARGIN, rightMargin=R_MARGIN,
        topMargin=10*mm, bottomMargin=16*mm,
    )
    story = []

    story.append(HeaderBanner(
        '✈  Flight Finder Advanced  —  Open-jaw Results',
        f'AI Flight Finder Advanced  ·  Live Google Flights prices  ·  '
        f'{datetime.now().strftime("%d %b %Y %H:%M")}',
        query, CONTENT_W,
    ))
    story.append(Spacer(1, 5*mm))

    if summary:
        _section('AI Travel Summary', story, S)
        story.append(Paragraph(summary, S['summary']))
        story.append(Spacer(1, 3*mm))

    _section(f'Top {min(len(trips), 15)} Trip Combinations  (ranked by total cost)', story, S)

    for i, trip in enumerate(trips[:15], 1):
        out_d = _flight_to_dict(trip.outbound)
        inb_d = _flight_to_dict(trip.inbound)
        total_str = f'TOTAL: £{trip.total_price:,.0f}'

        block = [
            TripDivider(f'#{i}', total_str, CONTENT_W),
            Spacer(1, 1*mm),
            FlightCard(i, out_d, CONTENT_W, accent=TEAL,
                       leg_label='OUTBOUND', show_rank=False,
                       bike_fee=out_d.get('_bike_fee')),
            Spacer(1, 1*mm),
            FlightCard(i, inb_d, CONTENT_W, accent=colors.HexColor('#7B3FA0'),
                       leg_label='RETURN', show_rank=False,
                       bike_fee=inb_d.get('_bike_fee')),
            Spacer(1, 3*mm),
        ]
        story.append(KeepTogether(block))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return filename


def export_friends(query: str, trips: list, summary: str,
                   traveller_names: list,
                   filename: str = 'flight_results_friends.pdf') -> str:
    """
    Export results from FlightFinderFriends.py (group trips).
    `trips` is a list of GroupTrip objects.
    `traveller_names` is a list of name strings (for colour assignment).
    Returns the output filename.
    """
    S = _styles()
    doc = SimpleDocTemplate(
        filename, pagesize=A4,
        leftMargin=L_MARGIN, rightMargin=R_MARGIN,
        topMargin=10*mm, bottomMargin=16*mm,
    )

    # Assign a colour to each traveller
    colour_map = {
        name: TRAVELLER_PALETTE[i % len(TRAVELLER_PALETTE)]
        for i, name in enumerate(traveller_names)
    }

    story = []
    story.append(HeaderBanner(
        '✈  Flight Finder Friends  —  Group Trip Results',
        f'AI Flight Finder Friends  ·  Live Google Flights  ·  '
        f'{datetime.now().strftime("%d %b %Y %H:%M")}',
        query, CONTENT_W,
    ))
    story.append(Spacer(1, 5*mm))

    if summary:
        _section('AI Travel Summary', story, S)
        story.append(Paragraph(summary, S['summary']))
        story.append(Spacer(1, 3*mm))

    _section(
        f'Top {min(len(trips), 10)} Group Trip Combinations  '
        f'(scored on total cost + arrival/departure sync)',
        story, S
    )

    for i, trip in enumerate(trips[:10], 1):
        total_str  = f'TOTAL: £{trip.total_cost:,.0f}  '
        n          = len(trip.outbound_legs)
        pp         = trip.total_cost / n if n else 0
        total_str += f'(£{pp:,.0f}/person)'

        block = [
            TripDivider(f'#{i}', total_str, CONTENT_W),
            Spacer(1, 1*mm),
            SyncBadge(trip.arrival_spread_mins,
                      trip.departure_spread_mins, CONTENT_W),
            Spacer(1, 1*mm),
        ]

        # Outbound legs — sort by arrive time so earliest is first
        out_sorted = sorted(trip.outbound_legs,
                            key=lambda f: getattr(f, 'arrive_minutes', 9999))
        for j, leg in enumerate(out_sorted):
            fd = _flight_to_dict(leg)
            tname = fd.get('traveller', '')
            tcol  = colour_map.get(tname, TEAL)
            lbl   = 'OUTBOUND' if j == 0 else ''
            block.append(FlightCard(
                i, fd, CONTENT_W,
                accent=tcol,
                leg_label=f'OUT · {tname}' if tname else 'OUTBOUND',
                traveller_name=tname,
                traveller_color=tcol,
                show_rank=False,
                bike_fee=fd.get('_bike_fee'),
            ))
            block.append(Spacer(1, 1*mm))

        # Return legs — sort by depart time
        inb_sorted = sorted(trip.inbound_legs,
                            key=lambda f: getattr(f, 'depart_minutes', 9999))
        for j, leg in enumerate(inb_sorted):
            fd = _flight_to_dict(leg)
            tname = fd.get('traveller', '')
            tcol  = colour_map.get(tname, colors.HexColor('#7B3FA0'))
            block.append(FlightCard(
                i, fd, CONTENT_W,
                accent=tcol,
                leg_label=f'RTN · {tname}' if tname else 'RETURN',
                traveller_name=tname,
                traveller_color=tcol,
                show_rank=False,
                bike_fee=fd.get('_bike_fee'),
            ))
            block.append(Spacer(1, 1*mm))

        block.append(Spacer(1, 3*mm))
        story.append(KeepTogether(block))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return filename
