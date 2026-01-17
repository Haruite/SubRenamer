import os
import shutil
import sys
import re
import traceback
import zipfile
from collections import defaultdict

import py7zr
import rarfile
from PyQt6.QtGui import QPainter, QColor
from PyQt6.QtWidgets import (
    QApplication, QWidget, QListWidget, QListWidgetItem,
    QHBoxLayout, QVBoxLayout, QSplitter, QLabel,
    QStyledItemDelegate, QComboBox, QRadioButton,
    QButtonGroup, QCheckBox, QPushButton, QMenu, QMessageBox
)
from PyQt6.QtCore import Qt, QSize, QTimer


ROW_HEIGHT = 25
DEFAULT_ROWS = 10
UNRAR_PATH = r"C:\Software\unrar.exe"

if not os.path.isfile(UNRAR_PATH):
    raise RuntimeError(f"unrar.exe 不存在: {UNRAR_PATH}")

rarfile.UNRAR_TOOL = UNRAR_PATH

LIST_WIDGETS = {}


def natural_key(text):
    return [
        int(p) if p.isdigit() else p.lower()
        for p in re.split(r'(\d+)', text)
    ]


VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts', '.wmv', '.flv'}

EP_PATTERN = re.compile(
    r'(?P<prefix>.+?)'
    r'(?:'
    r'\s*#(?P<ep1>\d+(?:\.\d+)?)(?=[\s\[(（]|$)'
    r'|'
    r'\s*\[(?P<ep2>\d+(?:\.\d+)?)(?:\([^]]+\))?\]'
    r')'
)

EXCLUDE_KEYWORDS = (
    '予告',
    'PV',
    'CM',
    'ノンクレジット',
    'メイキング',
    '特典',
    'SP',
    'OVA',
    'OAD'
)


# 字幕语言归一化
LANG_ALIAS = {
    "GB": "SC",
    "SC": "SC",
    "CHS": "SC",
    "BIG5": "TC",
    "TC": "TC",
    "CHT": "TC",
}

# 提取 集数 + 语言后缀
SUB_EP_LANG_PATTERN = re.compile(
    r'\[(?P<ep>\d{1,3})\].*\[(?P<lang>GB|BIG5|SC|TC|CHS|CHT)\]',
    re.IGNORECASE
)


def cluster_by_size(files):
    files = sorted(files, key=lambda x: x[1])
    clusters, current = [], []

    for f in files:
        if not current:
            current.append(f)
            continue
        if f[1] / current[-1][1] < 1.5:
            current.append(f)
        else:
            clusters.append(current)
            current = [f]

    if current:
        clusters.append(current)
    return clusters


def find_main_videos(folder):
    files = []

    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
            if any(k in name for k in EXCLUDE_KEYWORDS):
                continue
            files.append((name, os.path.getsize(path)))

    size_clusters = cluster_by_size(files)

    result = []

    for cluster in size_clusters:
        groups = defaultdict(list)

        for name, size in cluster:
            m = EP_PATTERN.search(name)
            if not m:
                continue

            ep = m.group('ep1') or m.group('ep2')
            if ep is None:
                continue

            prefix = m.group('prefix').strip()
            groups[prefix].append((float(ep), name))

        for group in groups.values():
            if len(group) >= 6:   # 正片最少集数
                result.extend(sorted(group, key=lambda x: x[0]))

    return [name for _, name in result]


def parse_subtitle_suffix(name: str):
    stem, ext = os.path.splitext(name)

    parts = stem.split('.')
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return stem, ''


def get_files_from_archive(archive_path: str) -> list[str]:
    base_dir = os.path.dirname(os.path.abspath(archive_path))
    base_name = os.path.splitext(os.path.basename(archive_path))[0]
    extract_dir = os.path.join(base_dir, base_name)

    os.makedirs(extract_dir, exist_ok=True)
    ext = os.path.splitext(archive_path)[1].lower()

    # ========= 解压 =========
    if ext == '.zip':
        with zipfile.ZipFile(archive_path, 'r') as z:
            z.extractall(extract_dir)

    elif ext == '.rar':
        with rarfile.RarFile(archive_path) as r:
            r.extractall(extract_dir)

    elif ext == '.7z':
        with py7zr.SevenZipFile(archive_path, mode='r') as z:
            z.extractall(path=extract_dir)

    print(f'解压完成 → {extract_dir}')

    # ========= 收集目录 → 文件 =========
    folder_to_files: dict[str, list[str]] = {}

    for root, dirs, files in os.walk(extract_dir):
        real_files = [
            os.path.join(root, f)
            for f in files
            if os.path.isfile(os.path.join(root, f))
        ]
        if real_files:
            folder_to_files[root] = real_files

    if not folder_to_files:
        return []

    # ========= 只有一个目录 =========
    if len(folder_to_files) == 1:
        return next(iter(folder_to_files.values()))

    # ========= 多个目录，弹窗选择 =========
    box = QMessageBox()
    box.setWindowTitle("选择字幕所在目录")
    box.setText("检测到压缩包内包含多个目录，请选择字幕所在路径：")
    box.setIcon(QMessageBox.Icon.Question)

    buttons = {}
    for folder in sorted(folder_to_files.keys()):
        # 显示相对路径，更友好
        display = os.path.relpath(folder, extract_dir)
        btn = box.addButton(display, QMessageBox.ButtonRole.AcceptRole)
        buttons[btn] = folder

    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.exec()

    clicked = box.clickedButton()
    if clicked in buttons:
        return folder_to_files[buttons[clicked]]

    return []


def filter_subtitles_by_lang(files, preferred_lang_group):
    """
    files: List[str]  字幕文件路径
    preferred_lang_group: 'SC' 或 'TC'
    """
    groups = defaultdict(list)

    for path in files:
        name = os.path.basename(path)

        m = SUB_EP_LANG_PATTERN.search(name)
        if not m:
            continue

        ep = int(m.group('ep'))
        raw_lang = m.group('lang').upper()

        lang_group = detect_lang_from_name(name)
        if not lang_group:
            continue
        groups[ep].append((lang_group, path))

    result = []

    for ep in sorted(groups):
        # 只选用户需要的语言
        for lang_group, path in groups[ep]:
            if lang_group == preferred_lang_group:
                result.append(path)
                break

    return result


def choose_subtitle_language(parent, langs):
    """
    langs: {'SC', 'TC'}
    return: 'SC' / 'TC' / None
    """
    box = QMessageBox(parent)
    box.setWindowTitle("选择字幕语言")
    box.setText("检测到多种字幕语言，请选择要导入的一种：")
    box.setIcon(QMessageBox.Icon.Question)

    buttons = {}

    if "SC" in langs:
        btn = box.addButton("简体（GB / SC / CHS）", QMessageBox.ButtonRole.AcceptRole)
        buttons[btn] = "SC"

    if "TC" in langs:
        btn = box.addButton("繁体（BIG5 / TC / CHT）", QMessageBox.ButtonRole.AcceptRole)
        buttons[btn] = "TC"

    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)

    box.exec()

    return buttons.get(box.clickedButton())


def apply_subtitle_language_filter(parent, files):
    langs_found = set()

    for p in files:
        name = os.path.basename(p)
        lang_group = detect_lang_from_name(name)
        if not lang_group:
            continue
        langs_found.add(lang_group)

    if len(langs_found) <= 1:
        return files

    selected = choose_subtitle_language(parent, langs_found)
    if not selected:
        return []

    return filter_subtitles_by_lang(files, selected)


def detect_lang_from_name(name: str):
    # ① 标签型 [CHS][CHT]
    m = SUB_EP_LANG_PATTERN.search(name)
    if m:
        raw = m.group('lang').upper()
        return LANG_ALIAS.get(raw)

    # ② 后缀型 .chs .cht .sc .tc
    lower = name.lower()
    if lower.endswith(('.chs', '.sc', '.gb')):
        return 'SC'
    if lower.endswith(('.cht', '.tc', '.big5')):
        return 'TC'

    return None


class FixedHeightDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), ROW_HEIGHT)


class FolderListWidget(QListWidget):
    def __init__(self, parent=None, name=None):
        super().__init__()
        self.parent = parent
        LIST_WIDGETS[name] = self
        self.name = name

        self.sort_ascending = True
        self.target_row = -1

        self.setItemDelegate(FixedHeightDelegate(self))

        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        if self.name in ('video', 'subtitle'):
            self.customContextMenuRequested.connect(self.show_context_menu)
            self.setDragEnabled(True)
            self.setAcceptDrops(True)
            self.setDropIndicatorShown(False)
            self.setDragDropMode(QListWidget.DragDropMode.DragDrop)

        self.init_placeholder()

        self.setStyleSheet("""
        QListWidget {
            border: 1px solid #999;
        }
        QListWidget::item {
            border-bottom: 1px solid #ccc;
            padding-left: 6px;
        }
        QListWidget::item:selected {
            background: #cce8ff;
        }
        """)

    # ========= Item 工具 =========
    def create_placeholder_item(self):
        item = QListWidgetItem(" ")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    def create_file_item(self, text, full_path=None):
        item = QListWidgetItem(text)
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled |
            Qt.ItemFlag.ItemIsSelectable |
            Qt.ItemFlag.ItemIsDragEnabled
        )
        if full_path:
            item.setData(Qt.ItemDataRole.UserRole, full_path)
        return item

    def is_real_item(self, item: QListWidgetItem):
        return item and item.text().strip()

    # ========= 初始化 =========
    def init_placeholder(self):
        self.clear()
        for _ in range(DEFAULT_ROWS):
            self.addItem(self.create_placeholder_item())

    def fill_placeholder(self):
        real = sum(1 for i in range(self.count()) if self.is_real_item(self.item(i)))
        while self.count() < max(DEFAULT_ROWS, real):
            self.addItem(self.create_placeholder_item())

    # ========= 拖拽逻辑 =========
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.source() is self:
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.source() is self:
            row = self.indexAt(event.position().toPoint()).row()
            if row != self.target_row:
                self.target_row = row
                self.viewport().update()
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            event.acceptProposedAction()

    def line_count(self):
        return sum(
            1 for i in range(self.count())
            if self.is_real_item(self.item(i))
        )

    def dropEvent(self, event):
        # ===== 内部拖拽排序 =====
        if event.source() is self:
            drag_row = self.currentRow()
            drop_row = self.indexAt(event.position().toPoint()).row()

            if drag_row < 0:
                return

            if drop_row < 0:
                drop_row = self.count() - 1

            drag_item = self.item(drag_row)
            if not self.is_real_item(drag_item):
                return

            if drag_row != drop_row:
                item = self.takeItem(drag_row)
                self.insertItem(drop_row, item)

            self.target_row = -1

            if self.name == 'video':
                self.reload_results()

            self.viewport().update()
            self.fill_placeholder()
            event.acceptProposedAction()
            return

        # ===== 外部文件 / 文件夹拖入 =====
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls()]
            if self.name != 'subtitle':
                self.clear()

            if len(paths) == 1 and os.path.isdir(paths[0]):
                folder = paths[0]

                # ===== 视频逻辑不变 =====
                if self.name == 'video':
                    files = find_main_videos(folder)
                    for f in files:
                        full = os.path.join(folder, f)
                        if os.path.isfile(full):
                            self.addItem(self.create_file_item(f, full))

                # ===== 字幕文件夹智能分析 =====
                elif self.name == 'subtitle':
                    suffix_map = defaultdict(list)

                    for name in os.listdir(folder):
                        full = os.path.join(folder, name)
                        if not os.path.isfile(full):
                            continue

                        base, suf = parse_subtitle_suffix(name)
                        if base is None:
                            continue

                        suffix_map[suf].append(name)

                    if not suffix_map:
                        return

                    mode = getattr(self, 'add_mode_combo', None)
                    has_real_items = self.line_count() > 0

                    if not (mode and mode.currentText() == "添加新文件：追加" and has_real_items):
                        # 覆盖模式，或者追加但原本没有真实字幕
                        self.clear()
                    else:
                        # 真正的“追加”
                        self.remove_leading_placeholders()

                    # ========= 语言检测（新增） =========
                    all_files = []
                    for suf, names in suffix_map.items():
                        for name in names:
                            all_files.append(os.path.join(folder, name))

                    all_files = apply_subtitle_language_filter(self, all_files)
                    if not all_files:
                        return

                    # ========= 重新构建 suffix_map =========
                    suffix_map.clear()
                    for p in all_files:
                        name = os.path.basename(p)
                        base, suf = parse_subtitle_suffix(name)
                        suffix_map[suf].append(name)

                    # ========= 后缀选择 =========
                    if len(suffix_map) == 1:
                        selected_suffix = next(iter(suffix_map))
                    else:
                        selected_suffix = self.choose_subtitle_suffix(suffix_map.keys())
                        if selected_suffix is None:
                            return

                    for name in suffix_map[selected_suffix]:
                        full = os.path.join(folder, name)
                        self.addItem(self.create_file_item(name, full))

            else:
                if len(paths) == 1 and paths[0].lower().endswith(('.zip', '.rar', '.7z')):
                    archive_path = paths[0]

                    QTimer.singleShot(
                        0,
                        lambda p=archive_path: self._handle_subtitle_archive(p)
                    )

                    event.acceptProposedAction()
                    return  # ★★★ 必须立刻返回，阻止 fill_placeholder
                else:
                    for p in paths:
                        if os.path.isfile(p):
                            name = os.path.basename(p)
                            self.addItem(self.create_file_item(name, p))

                self.fill_placeholder()
                event.acceptProposedAction()
                self.reload_results()
                return

            self.reload_results()

            self.fill_placeholder()
            event.acceptProposedAction()

    def reload_results(self):
        if LIST_WIDGETS['video'].line_count() > 0 and LIST_WIDGETS['subtitle'].line_count() > 0:
            for _ in range(LIST_WIDGETS['video'].line_count()):
                if _ < LIST_WIDGETS['subtitle'].line_count():
                    name = os.path.splitext(LIST_WIDGETS['video'].item(_).text())[0]
                    suf = os.path.splitext(LIST_WIDGETS['subtitle'].item(_).text())[1]
                    text = name + self.parent.suffix_combo.currentText().strip() + suf
                    sub_item = LIST_WIDGETS['subtitle'].item(_)
                    sub_path = sub_item.data(Qt.ItemDataRole.UserRole) if sub_item else None

                    item = None
                    if _ < LIST_WIDGETS['result'].count():
                        item = LIST_WIDGETS['result'].item(_)
                        item.setText(text)
                    else:
                        item = self.create_file_item(text)
                        LIST_WIDGETS['result'].addItem(item)

                    # ★ 关键：把字幕真实路径绑定到 result
                    if sub_path:
                        item.setData(Qt.ItemDataRole.UserRole, sub_path)
                elif _ < LIST_WIDGETS['result'].count():
                    LIST_WIDGETS['result'].item(_).setText('')

            for i in range(LIST_WIDGETS['result'].count()):
                item = LIST_WIDGETS['result'].item(i)
                if item and item.text().strip():
                    item.setForeground(QColor("#1565c0"))

            QTimer.singleShot(500, self._reset_result_color)
        else:
            for _ in range(LIST_WIDGETS['result'].count()):
                LIST_WIDGETS['result'].item(_).setText('')

    def _reset_result_color(self):
        for i in range(LIST_WIDGETS['result'].count()):
            item = LIST_WIDGETS['result'].item(i)
            if item:
                item.setForeground(QColor("#000000"))

    # ========= 插入线绘制 =========
    def paintEvent(self, event):
        super().paintEvent(event)

        if self.target_row < 0:
            return

        item = self.item(self.target_row)
        if not item:
            return

        rect = self.visualItemRect(item)
        painter = QPainter(self.viewport())
        painter.setPen(QColor(10, 240, 10))
        painter.drawLine(rect.left(), rect.top(), rect.right(), rect.top())

    # ========= 右键删除 =========
    def show_context_menu(self, pos):
        items = self.selectedItems()

        # 如果右键的那一行没被选中，则只删除这一行
        item = self.itemAt(pos)
        if item and item not in items:
            self.clearSelection()
            item.setSelected(True)
            items = [item]

        # 没有真实文件就不弹菜单
        real_items = [i for i in items if self.is_real_item(i)]
        if not real_items:
            return

        menu = QMenu(self)
        delete_action = menu.addAction(f"删除 ({len(real_items)})")

        if menu.exec(self.mapToGlobal(pos)) == delete_action:
            # 倒序删除，避免行号变化
            for item in sorted(real_items, key=self.row, reverse=True):
                self.takeItem(self.row(item))

            self.fill_placeholder()
            self.reload_results()

    def sort_files(self):
        names = [
            self.item(i).text()
            for i in range(self.count())
            if self.is_real_item(self.item(i))
        ]

        names.sort(
            key=natural_key,
            reverse=not self.sort_ascending
        )

        self.clear()
        for name in names:
            self.addItem(self.create_file_item(name))

        self.sort_ascending = not self.sort_ascending
        self.fill_placeholder()
        if self.name == 'video':
            self.reload_results()

    def clear_files(self):
        self.clear()

        self.sort_ascending = True
        self.fill_placeholder()
        self.reload_results()

    def choose_subtitle_suffix(self, suffixes):
        box = QMessageBox(self)
        box.setWindowTitle("选择字幕后缀")
        box.setText("检测到多种字幕后缀，请选择要导入的一种：")
        box.setIcon(QMessageBox.Icon.Question)

        buttons = {}
        for suf in sorted(suffixes):
            text = suf if suf else "(无后缀)"
            btn = box.addButton(text, QMessageBox.ButtonRole.AcceptRole)
            buttons[btn] = suf

        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        return buttons.get(clicked)

    def remove_leading_placeholders(self):
        """删除前面的占位空行"""
        while self.count() > 0:
            item = self.item(0)
            if item and not self.is_real_item(item):
                self.takeItem(0)
            else:
                break

    def _handle_subtitle_archive(self, archive_path):
        try:
            files = get_files_from_archive(archive_path)
            if not files:
                return

            # ========= 语言检测 =========
            langs_found = set()
            for p in files:
                lang = detect_lang_from_name(os.path.basename(p))
                if lang:
                    langs_found.add(lang)

            if len(langs_found) > 1:
                files = apply_subtitle_language_filter(self, files)
                if not files:
                    return

            # ========= 追加 / 覆盖 =========
            mode = getattr(self, 'add_mode_combo', None)

            has_real_items = self.line_count() > 0

            if (
                    mode
                    and mode.currentText() == "添加新文件：追加"
                    and has_real_items
            ):
                # 真正有字幕 → 才是追加
                self.remove_leading_placeholders()
            else:
                # 没有字幕 or 覆盖模式 → 当成全新导入
                self.clear()

            # ========= 构建 suffix_map =========
            suffix_map = defaultdict(list)
            for p in files:
                name = os.path.basename(p)
                _, suf = parse_subtitle_suffix(name)
                suffix_map[suf].append(p)

            # ========= 后缀选择 =========
            if len(suffix_map) == 1:
                selected_suffix = next(iter(suffix_map))
            else:
                selected_suffix = self.choose_subtitle_suffix(suffix_map.keys())
                if selected_suffix is None:
                    return

            # ========= 添加到列表（关键） =========
            for p in suffix_map[selected_suffix]:
                if os.path.isfile(p):
                    name = os.path.basename(p)
                    self.addItem(self.create_file_item(name, p))

            self.fill_placeholder()
            self.reload_results()

        except Exception:
            traceback.print_exc()


def build_header(title, with_tools=False, list_widget=None, extra_widget=None):
    header = QWidget()
    header.setFixedHeight(36)

    layout = QHBoxLayout(header)
    layout.setContentsMargins(6, 2, 6, 2)
    layout.setSpacing(6)

    label = QLabel(title)
    label.setStyleSheet("font-weight: bold;")
    layout.addWidget(label)
    layout.addStretch()

    if with_tools and list_widget:
        btn_sort = QPushButton("排序")
        btn_clear = QPushButton("清空")
        btn_sort.clicked.connect(list_widget.sort_files)
        btn_clear.clicked.connect(list_widget.clear_files)
        layout.addWidget(btn_sort)
        layout.addWidget(btn_clear)

    if extra_widget:
        layout.addWidget(extra_widget)

    return header


# ================== 三个 Column ==================
class VideoColumn(QWidget):
    def __init__(self, parent=None):
        super().__init__()
        self.list = FolderListWidget(parent,'video')
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        header = build_header("视频文件", True, self.list)
        layout.addWidget(header)
        layout.addWidget(self.list)


class SubtitleColumn(QWidget):
    def __init__(self, parent=None):
        super().__init__()
        self.list = FolderListWidget(parent,'subtitle')

        self.add_mode_combo = QComboBox()
        self.add_mode_combo.addItems(["添加新文件：追加", "添加新文件：覆盖"])

        self._build()
        self.list.add_mode_combo = self.add_mode_combo

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        header = build_header(
            "字幕文件",
            True,
            self.list,
            extra_widget=self.add_mode_combo
        )
        layout.addWidget(header)
        layout.addWidget(self.list)


class ResultColumn(QWidget):
    def __init__(self, parent=None):
        super().__init__()
        self.list = FolderListWidget(parent, 'result')
        self.list.setDragEnabled(False)
        self.list.setAcceptDrops(False)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        header = build_header("重命名为")
        layout.addWidget(header)
        layout.addWidget(self.list)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("U2字幕重命名工具")
        self.resize(1000, 520)

        main_layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.video = VideoColumn(self)
        self.sub = SubtitleColumn(self)
        self.result = ResultColumn(self)

        splitter.addWidget(self.video)
        splitter.addWidget(self.sub)
        splitter.addWidget(self.result)
        splitter.setSizes([333, 333, 334])

        main_layout.addWidget(splitter)

        bottom = QHBoxLayout()

        bottom.addWidget(QLabel("添加后缀："))
        self.suffix_combo = QComboBox()
        self.suffix_combo.setEditable(True)
        self.suffix_combo.addItems(
            ["", ".chs", ".cht", ".sc", ".tc", ".big5", ".gb"]
        )
        bottom.addWidget(self.suffix_combo)
        self.suffix_combo.currentTextChanged.connect(self.video.list.reload_results)

        bottom.addStretch()

        self.radio_rename = QRadioButton("直接重命名字幕")
        self.radio_copy = QRadioButton("复制字幕到动画目录")
        self.radio_copy.setChecked(True)

        group = QButtonGroup(self)
        group.addButton(self.radio_rename)
        group.addButton(self.radio_copy)

        bottom.addWidget(self.radio_rename)
        bottom.addWidget(self.radio_copy)

        bottom.addStretch()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "color: #2e7d32; font-weight: bold;"
        )
        self.status_label.hide()

        bottom.addWidget(self.status_label)

        self.apply_btn = QPushButton("应用")
        self.apply_btn.setFixedWidth(80)
        self.apply_btn.clicked.connect(self.on_apply)
        bottom.addWidget(self.apply_btn)

        self.top_checkbox = QCheckBox("置顶")
        self.top_checkbox.toggled.connect(self.on_top_changed)
        bottom.addWidget(self.top_checkbox)

        main_layout.addLayout(bottom)

    def on_top_changed(self, checked):
        flags = self.windowFlags()
        if checked:
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def get_real_items(self, list_widget: FolderListWidget):
        items = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item and item.text().strip():
                items.append(item.text())
        return items

    def on_apply(self):
        success_count, failed_count, skip_count = 0, 0, 0
        for i in range(self.result.list.count()):
            if self.result.list.is_real_item(self.result.list.item(i)):
                text = self.result.list.item(i).text()
                if text.strip():
                    try:
                        result_item = self.result.list.item(i)
                        sub_path = result_item.data(Qt.ItemDataRole.UserRole)
                        if not sub_path:
                            continue
                        folder_path = os.path.dirname(os.path.normpath(sub_path))
                        new_name = self.result.list.item(i).text()
                        new_path = os.path.normpath(os.path.join(folder_path, new_name))
                        if sub_path != new_path:
                            os.rename(sub_path, new_path)
                            print(f'重命名字幕文件：{sub_path} -> {new_path}')

                            if self.radio_copy.isChecked():
                                video_path = self.video.list.item(i).data(Qt.ItemDataRole.UserRole)
                                video_folder = os.path.dirname(os.path.normpath(video_path))
                                new_sub = os.path.join(video_folder, new_name)
                                shutil.copy(new_path, new_sub)
                                print(f'复制字幕文件：{new_sub} -> {new_sub}')

                            success_count += 1
                        else:
                            skip_count+=1
                    except Exception:
                        traceback.print_exc()
                        failed_count += 1

        for i in range(self.sub.list.count()):
            self.sub.list.item(i).setData(Qt.ItemDataRole.UserRole, '')
            self.sub.list.item(i).setText('')

        for i in range(self.result.list.count()):
            self.result.list.item(i).setText('')

        self.status_label.setText(
            f"重命名操作完成，成功 {success_count} 个，失败 {failed_count} 个，跳过 {skip_count} 个"
        )
        self.status_label.show()

        # 5 秒后清除
        QTimer.singleShot(5000, self.status_label.clear)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
