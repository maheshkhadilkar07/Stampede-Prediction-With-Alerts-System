"""
utils/report_generator.py
============================
Builds a one-page PDF summary report for a completed video analysis,
using reportlab (no external binaries needed, unlike wkhtmltopdf).

Save this file at: Stampede-Prediction-System/utils/report_generator.py
"""

import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def generate_video_report_pdf(video) -> str:
    """
    Build a PDF report for a `database.models.Video` row and save it to
    config.REPORTS_DIR. Returns the absolute path of the generated file.
    """
    filename = f"{config.REPORT_FILENAME_PREFIX}{video.id}.pdf"
    output_path = os.path.join(config.REPORTS_DIR, filename)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], textColor=colors.HexColor("#1a1a2e")
    )
    heading_style = ParagraphStyle(
        "ReportHeading", parent=styles["Heading2"], textColor=colors.HexColor("#dc3545")
    )

    doc = SimpleDocTemplate(output_path, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm)
    elements = []

    elements.append(Paragraph("AI-Based Stampede Prediction Report", title_style))
    elements.append(Spacer(1, 6))
    elements.append(
        Paragraph(
            f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            styles["Normal"],
        )
    )
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("Video Details", heading_style))
    details_data = [
        ["Original Filename", video.original_filename],
        ["Source Type", video.source_type.upper()],
        ["Uploaded At", video.upload_time.strftime("%Y-%m-%d %H:%M:%S")],
        ["Resolution", f"{video.frame_width} x {video.frame_height}"],
        ["Total Frames", str(video.total_frames)],
        ["Source FPS", f"{video.source_fps:.2f}" if video.source_fps else "N/A"],
        ["Processing Time (s)", f"{video.processing_time_seconds:.2f}" if video.processing_time_seconds else "N/A"],
    ]
    details_table = Table(details_data, colWidths=[6 * cm, 9 * cm])
    details_table.setStyle(_default_table_style())
    elements.append(details_table)
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("Crowd Analysis Summary", heading_style))
    summary_data = [
        ["Peak Person Count", str(video.peak_count or 0)],
        ["Average Person Count", f"{video.average_count:.2f}" if video.average_count else "0.00"],
        ["Final Risk Level", video.final_risk_level or "SAFE"],
    ]
    summary_table = Table(summary_data, colWidths=[6 * cm, 9 * cm])
    summary_table.setStyle(_default_table_style())
    elements.append(summary_table)
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("Risk Timeline (per sampled second)", heading_style))
    timeline = video.get_risk_timeline()
    if timeline:
        timeline_data = [["Second", "Risk Level"]] + [
            [str(entry["second"]), entry["level"]] for entry in timeline
        ]
        timeline_table = Table(timeline_data, colWidths=[6 * cm, 9 * cm])
        timeline_table.setStyle(_default_table_style(header=True))
        elements.append(timeline_table)
    else:
        elements.append(Paragraph("No timeline data recorded.", styles["Normal"]))

    doc.build(elements)
    logger.info(f"Generated PDF report for video {video.id} at {output_path}")
    return output_path


def _default_table_style(header: bool = False) -> TableStyle:
    """Shared table styling so every table in the report looks consistent."""
    style_commands = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    if header:
        style_commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")))
        style_commands.append(("TEXTCOLOR", (0, 0), (-1, 0), colors.white))
        style_commands.append(("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"))
    else:
        style_commands.append(("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")))
        style_commands.append(("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"))
    return TableStyle(style_commands)
