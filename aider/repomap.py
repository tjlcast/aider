import colorsys
import math
import os
import random
import shutil
import sqlite3
import sys
import time
import warnings
from collections import Counter, defaultdict, namedtuple
from importlib import resources
from pathlib import Path

from diskcache import Cache
from grep_ast import TreeContext, filename_to_lang
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from tqdm import tqdm

from aider.dump import dump
from aider.special import filter_important_files
from aider.waiting import Spinner

# tree_sitter is throwing a FutureWarning
warnings.simplefilter("ignore", category=FutureWarning)
from grep_ast.tsl import USING_TSL_PACK, get_language, get_parser  # noqa: E402

Tag = namedtuple("Tag", "rel_fname fname line name kind".split())


SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError, OSError)


CACHE_VERSION = 3
if USING_TSL_PACK:
    CACHE_VERSION = 4

UPDATING_REPO_MAP_MESSAGE = "Updating repo map"


class RepoMap:
    TAGS_CACHE_DIR = f".aider.tags.cache.v{CACHE_VERSION}"

    warned_files = set()

    def __init__(
        self,
        map_tokens=1024,
        root=None,
        main_model=None,
        io=None,
        repo_content_prefix=None,
        verbose=False,
        max_context_window=None,
        map_mul_no_files=8,
        refresh="auto",
    ):
        self.io = io
        self.verbose = verbose
        self.refresh = refresh

        if not root:
            root = os.getcwd()
        self.root = root

        self.load_tags_cache()
        self.cache_threshold = 0.95

        self.max_map_tokens = map_tokens
        self.map_mul_no_files = map_mul_no_files
        self.max_context_window = max_context_window

        self.repo_content_prefix = repo_content_prefix

        self.main_model = main_model

        self.tree_cache = {}
        self.tree_context_cache = {}
        self.map_cache = {}
        self.map_processing_time = 0
        self.last_map = None

        if self.verbose:
            self.io.tool_output(
                f"RepoMap initialized with map_mul_no_files: {self.map_mul_no_files}"
            )

    def token_count(self, text):
        len_text = len(text)
        if len_text < 200:
            return self.main_model.token_count(text)

        lines = text.splitlines(keepends=True)
        num_lines = len(lines)
        step = num_lines // 100 or 1
        lines = lines[::step]
        sample_text = "".join(lines)
        sample_tokens = self.main_model.token_count(sample_text)
        est_tokens = sample_tokens / len(sample_text) * len_text
        return est_tokens

    def get_repo_map(
        self,
        chat_files,
        other_files,
        mentioned_fnames=None,
        mentioned_idents=None,
        force_refresh=False,
    ):
        if self.max_map_tokens <= 0:
            return
        if not other_files:
            return
        if not mentioned_fnames:
            mentioned_fnames = set()
        if not mentioned_idents:
            mentioned_idents = set()

        max_map_tokens = self.max_map_tokens

        # With no files in the chat, give a bigger view of the entire repo
        padding = 4096
        if max_map_tokens and self.max_context_window:
            target = min(
                int(max_map_tokens * self.map_mul_no_files),
                self.max_context_window - padding,
            )
        else:
            target = 0
        if not chat_files and self.max_context_window and target > 0:
            max_map_tokens = target

        try:
            files_listing = self.get_ranked_tags_map(
                chat_files,
                other_files,
                max_map_tokens,
                mentioned_fnames,
                mentioned_idents,
                force_refresh,
            )
        except RecursionError:
            self.io.tool_error("Disabling repo map, git repo too large?")
            self.max_map_tokens = 0
            return

        if not files_listing:
            return

        if self.verbose:
            num_tokens = self.token_count(files_listing)
            self.io.tool_output(f"Repo-map: {num_tokens / 1024:.1f} k-tokens")

        if chat_files:
            other = "other "
        else:
            other = ""

        if self.repo_content_prefix:
            repo_content = self.repo_content_prefix.format(other=other)
        else:
            repo_content = ""

        repo_content += files_listing

        return repo_content

    def get_rel_fname(self, fname):
        try:
            return os.path.relpath(fname, self.root)
        except ValueError:
            # Issue #1288: ValueError: path is on mount 'C:', start on mount 'D:'
            # Just return the full fname.
            return fname

    def tags_cache_error(self, original_error=None):
        """Handle SQLite errors by trying to recreate cache, falling back to dict if needed"""

        if self.verbose and original_error:
            self.io.tool_warning(f"Tags cache error: {str(original_error)}")

        if isinstance(getattr(self, "TAGS_CACHE", None), dict):
            return

        path = Path(self.root) / self.TAGS_CACHE_DIR

        # Try to recreate the cache
        try:
            # Delete existing cache dir
            if path.exists():
                shutil.rmtree(path)

            # Try to create new cache
            new_cache = Cache(path)

            # Test that it works
            test_key = "test"
            new_cache[test_key] = "test"
            _ = new_cache[test_key]
            del new_cache[test_key]

            # If we got here, the new cache works
            self.TAGS_CACHE = new_cache
            return

        except SQLITE_ERRORS as e:
            # If anything goes wrong, warn and fall back to dict
            self.io.tool_warning(
                f"Unable to use tags cache at {path}, falling back to memory cache"
            )
            if self.verbose:
                self.io.tool_warning(f"Cache recreation error: {str(e)}")

        self.TAGS_CACHE = dict()

    def load_tags_cache(self):
        path = Path(self.root) / self.TAGS_CACHE_DIR
        try:
            self.TAGS_CACHE = Cache(path)
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)

    def save_tags_cache(self):
        pass

    def get_mtime(self, fname):
        try:
            return os.path.getmtime(fname)
        except FileNotFoundError:
            self.io.tool_warning(f"File not found error: {fname}")

    def get_tags(self, fname, rel_fname):
        # Check if the file is in the cache and if the modification time has not changed
        file_mtime = self.get_mtime(fname)
        if file_mtime is None:
            return []

        cache_key = fname
        try:
            val = self.TAGS_CACHE.get(cache_key)  # Issue #1308
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            val = self.TAGS_CACHE.get(cache_key)

        if val is not None and val.get("mtime") == file_mtime:
            try:
                return self.TAGS_CACHE[cache_key]["data"]
            except SQLITE_ERRORS as e:
                self.tags_cache_error(e)
                return self.TAGS_CACHE[cache_key]["data"]

        # miss!
        data = list(self.get_tags_raw(fname, rel_fname))

        # Update the cache
        try:
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}
            self.save_tags_cache()
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}

        return data

    def get_tags_raw(self, fname, rel_fname):
        lang = filename_to_lang(fname)              # 根据文件后缀确定语言类型（如 .py → python）
        if not lang:
            return                                      # 跳过无法识别的文件类型

        try:
            language = get_language(lang)           # 获取tree-sitter语言对象
            parser = get_parser(lang)               # 获取对应语言的解析器
        except Exception as err:
            print(f"Skipping file {fname}: {err}")      # 解析器初始化失败时跳过
            return

        query_scm = get_scm_fname(lang)             # 获取对应语言的.scm查询文件路径
        if not query_scm.exists():
            return                                      # 无查询规则文件时跳过
        query_scm = query_scm.read_text()           # 读取.scm文件内容
        """
        关键文件: queries/{lang}-tags.scm(定义如何提取tags)

        示例规则：

        scm
        (function_definition name: (identifier) @name.definition.function)
        """

        code = self.io.read_text(fname)             # 读取指定解析的源代码
        if not code:                                    # 空文件跳过
            return
        tree = parser.parse(bytes(code, "utf-8"))   # 生成语法树(使用 tree-sitter 解析器)
        """
        输入处理：源代码 → UTF-8字节流 → 语法树

        输出：tree-sitter的AST节点树
        """

        # Run the tags queries
        query = language.query(query_scm)           # 编译查询规则
        captures = query.captures(tree.root_node)   # 在AST上执行查询

        saw = set()
        if USING_TSL_PACK:  # 处理不同tree-sitter版本的返回格式
            all_nodes = []
            for tag, nodes in captures.items():
                all_nodes += [(node, tag) for node in nodes]
        else:
            all_nodes = list(captures)  # 旧版直接返回列表

        """
        查询结果：捕获的节点列表，每个节点包含：

        代码位置（行/列）

        标记类型（definition/reference）

        标识符名称

        """

        for node, tag in all_nodes:
            if tag.startswith("name.definition."):      # 定义节点（函数/类等）
                kind = "def"
            elif tag.startswith("name.reference."):     # 引用节点（函数调用等）
                kind = "ref"
            else:
                continue  # 跳过非目标标签

            saw.add(kind)

            result = Tag(                       # 生成Tag对象
                rel_fname=rel_fname,                # 相对路径
                fname=fname,                        # 绝对路径
                name=node.text.decode("utf-8"),     # 标识符名称
                kind=kind,                          # 类型(def/ref)
                line=node.start_point[0],           # 行号（0-based）
            )

            """
            Tag对象结构:
            Tag(rel_fname, fname, name, kind, line)
            """

            yield result

        if "ref" in saw:
            return
        if "def" not in saw:
            return
        """
        # 当只有定义没有引用时
        """

        # We saw defs, without any refs
        # Some tags files only provide defs (cpp, for example)
        # Use pygments to backfill refs

        try:
            lexer = guess_lexer_for_filename(fname, code)       # 使用pygments分词
        except Exception:  # On Windows, bad ref to time.clock which is deprecated?
            # self.io.tool_error(f"Error lexing {fname}")
            return

        tokens = list(lexer.get_tokens(code))
        tokens = [token[1] for token in tokens if token[0]
                  in Token.Name]  # if token[0] in Token.Name 只保留标识符

        for token in tokens:    # 生成补充的引用tags
            yield Tag(
                rel_fname=rel_fname,
                fname=fname,
                name=token,
                kind="ref",
                line=-1,        # line=-1表示未知行号
            )

    def get_ranked_tags(
        self, chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, progress=None
    ):
        import networkx as nx

        defines = defaultdict(set)          # {符号名: {定义该符号的文件集合}}
        references = defaultdict(list)      # {符号名: [引用该符号的文件列表]}
        definitions = defaultdict(set)      # {(文件名, 符号名): {对应的Tag对象}}

        personalization = dict()            # {文件名: 个性化权重}

        fnames = set(chat_fnames).union(set(other_fnames))  # 所有待分析文件
        chat_rel_fnames = set()

        fnames = sorted(fnames)
        """
        功能: 初始化数据结构, 计算基础权重

        设计: 使用defaultdict避免键检查
        """

        # Default personalization for unspecified files is 1/num_nodes
        # https://networkx.org/documentation/stable/_modules/networkx/algorithms/link_analysis/pagerank_alg.html#pagerank
        personalize = 100 / len(fnames)             # 默认个性化权重

        """
        关键点：
            defines 和 references 用于统计符号的定义和引用位置。
            personalization 用于调整 PageRank 计算时的文件权重（如聊天相关文件权重更高）。
        """

        try:
            cache_size = len(self.TAGS_CACHE)
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            cache_size = len(self.TAGS_CACHE)

        if len(fnames) - cache_size > 100:
            self.io.tool_output(
                "Initial repo scan can be slow in larger repos, but only happens once."
            )
            fnames = tqdm(fnames, desc="Scanning repo")
            showing_bar = True
        else:
            showing_bar = False

        for fname in fnames:
            if self.verbose:
                self.io.tool_output(f"Processing {fname}")
            if progress and not showing_bar:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {fname}")

            try:
                file_ok = Path(fname).is_file()
            except OSError:
                file_ok = False

            if not file_ok:
                if fname not in self.warned_files:
                    self.io.tool_warning(f"Repo-map can't include {fname}")
                    self.io.tool_output(
                        "Has it been deleted from the file system but not from git?"
                    )
                    self.warned_files.add(fname)
                continue

            # dump(fname)
            rel_fname = self.get_rel_fname(fname)   # 获取文件相对路径
            current_pers = 0.0  # Start with 0 personalization score

            # 计算文件的个性化权重（聊天相关文件、提及文件、路径匹配提及符号的文件）
            if fname in chat_fnames:
                current_pers += personalize
                chat_rel_fnames.add(rel_fname)

            if rel_fname in mentioned_fnames:
                # Use max to avoid double counting if in chat_fnames and mentioned_fnames
                current_pers = max(current_pers, personalize)

            # Check path components against mentioned_idents
            path_obj = Path(rel_fname)
            path_components = set(path_obj.parts)
            basename_with_ext = path_obj.name
            basename_without_ext, _ = os.path.splitext(basename_with_ext)
            components_to_check = path_components.union({basename_with_ext, basename_without_ext})

            matched_idents = components_to_check.intersection(mentioned_idents)
            if matched_idents:
                # Add personalization *once* if any path component matches a mentioned ident
                current_pers += personalize

            if current_pers > 0:
                personalization[rel_fname] = current_pers  # Assign the final calculated value

            # 提取文件的符号定义和引用
            tags = list(self.get_tags(fname, rel_fname))
            if tags is None:
                continue

            for tag in tags:
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)        # 记录定义位置
                    key = (rel_fname, tag.name)
                    definitions[key].add(tag)               # 存储Tag对象

                elif tag.kind == "ref":
                    references[tag.name].append(rel_fname)  # 记录引用位置
        """
        get_tags() 方法使用 tree-sitter 解析代码并提取符号。

        聊天相关文件（chat_fnames）和提及文件（mentioned_fnames）会被赋予更高的权重。
        """

        ##
        # dump(defines)
        # dump(references)
        # dump(personalization)

        if not references:
            references = dict((k, list(v)) for k, v in defines.items())

        idents = set(defines.keys()).intersection(set(references.keys()))

        G = nx.MultiDiGraph()

        # Add a small self-edge for every definition that has no references
        # Helps with tree-sitter 0.23.2 with ruby, where "def greet(name)"
        # isn't counted as a def AND a ref. tree-sitter 0.24.0 does.
        # 处理无引用的定义（添加自环边）
        for ident in defines.keys():
            if ident in references:
                continue
            for definer in defines[ident]:
                G.add_edge(definer, definer, weight=0.1, ident=ident)

        # 添加定义-引用边（带动态权重）
        for ident in idents:
            if progress:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {ident}")

            definers = defines[ident]

            mul = 1.0  # 初始乘数

            # 检查命名风格
            is_snake = ("_" in ident) and any(c.isalpha() for c in ident)  # 蛇形命名法，如 my_function
            is_camel = any(c.isupper() for c in ident) and any(c.islower()
                                                               for c in ident)  # 驼峰命名法，如 myFunction
            # 根据不同因素调整乘数
            if ident in mentioned_idents:
                mul *= 10  # 如果标识符被用户提及，权重增加10倍
            if (is_snake or is_camel) and len(ident) >= 8:
                mul *= 10  # 如果是蛇形或驼峰命名且长度>=8，权重增加10倍
            if ident.startswith("_"):
                mul *= 0.1  # 如果以下划线开头，权重减少到0.1倍
            if len(defines[ident]) > 5:
                mul *= 0.1  # 如果定义超过5个，权重减少到0.1倍

            for referencer, num_refs in Counter(references[ident]).items():
                for definer in definers:
                    # dump(referencer, definer, num_refs, mul)
                    # if referencer == definer:
                    #    continue

                    use_mul = mul  # 使用基础乘数

                    # 如果引用文件在聊天文件中，进一步增加权重
                    if referencer in chat_rel_fnames:
                        use_mul *= 50

                    # 对引用次数进行平方根缩放，避免高频引用主导
                    # scale down so high freq (low value) mentions don't dominate
                    num_refs = math.sqrt(num_refs)

                    # 添加边，权重为调整后的乘数乘以缩放后的引用次数
                    G.add_edge(referencer, definer, weight=use_mul * num_refs, ident=ident)
        """
        功能：

            构建代码依赖图，节点是文件，边表示符号的引用关系。

            动态调整边的权重（基于符号命名风格、是否被提及等）。

            关键点：

            使用 networkx.MultiDiGraph 存储图结构。

            边的权重影响 PageRank 计算结果（权重越高，重要性越高）。
        """
        if not references:
            pass

        if personalization:
            pers_args = dict(personalization=personalization, dangling=personalization)
        else:
            pers_args = dict()

        try:
            # ranked 的返回示例
            # {
            #   'aider/main.py': 0.0,
            #   'aider/repomap.py': 0.0,
            #   'aider/repomap_test.py': 0.0,
            #   'aider/utils.py': 0.0,
            # }
            ranked = nx.pagerank(G, weight="weight", **pers_args)
        except ZeroDivisionError:
            # Issue #1536
            try:
                ranked = nx.pagerank(G, weight="weight")
            except ZeroDivisionError:
                return []

        # distribute the rank from each source node, across all of its out edges
        # [排名权重分配]
        # 权重分配逻辑：
        #   1\ 对每个源节点(src)，获取其PageRank值(src_rank)
        #   2\ 计算该节点所有出边的总权重(total_weight)
        #   3\ 将节点的PageRank值按比例分配到各出边：
        #   4\ 每条边的分配值 = (src_rank × 边权重) / 总权重
        #   5\ 将分配值累加到目标节点和符号的组合上(ranked_definitions)
        # 数据结构：ranked_definitions是defaultdict，键为(目标文件名, 符号名)，值为累计的排名分数
        ranked_definitions = defaultdict(float)
        # ranked_definitions = [
        #     (("src/models/user.py", "User"), 0.1234),
        #     (("src/config.py", "config"), 0.0789),
        #     (("src/utils/math.py", "calculate_total"), 0.0456),
        #     (("tests/test_math.py", "test_calculate"), 0.0345),
        #     (("src/utils/validation.py", "_internal_check"), 0.0056)
        # ]
        for src in G.nodes:
            if progress:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {src}")

            src_rank = ranked[src]
            total_weight = sum(data["weight"] for _src, _dst, data in G.out_edges(src, data=True))
            # dump(src, src_rank, total_weight)
            for _src, dst, data in G.out_edges(src, data=True):
                data["rank"] = src_rank * data["weight"] / total_weight
                ident = data["ident"]
                ranked_definitions[(dst, ident)] += data["rank"]

        # 生成最终排序结果
        # ranked_tags = [
        #     # 高排名符号定义
        #     <Tag def calculate_sum in utils.py>,
        #     <Tag def calculate_sum in math_ops.py>,
        #     <Tag def helper_fn in helper.py>,
        #     # 没有符号的高排名文件
        #     ("repo/utils.py",),
        #     ("repo/helper.py",),
        #     ("repo/math_ops.py",)
        # ]
        ranked_tags = []
        ranked_definitions = sorted(
            ranked_definitions.items(), reverse=True, key=lambda x: (x[1], x[0])
        )

        # dump(ranked_definitions)
        # [生成最终排序结果]
        # 排序处理：
        #   按排名分数降序排序ranked_definitions
        #   跳过聊天相关文件(chat_rel_fnames)
        #   将符号定义添加到结果列表
        for (fname, ident), rank in ranked_definitions:
            # print(f"{rank:.03f} {fname} {ident}")
            if fname in chat_rel_fnames:
                continue
            ranked_tags += list(definitions.get((fname, ident), []))

        # [处理无符号定义的文件]
        # 处理逻辑：
        #   获取其他文件中没有符号定义的文件集合
        #   获取已包含在结果中的文件名集合
        #   按PageRank值排序所有文件
        #   将高排名但未包含的文件添加到结果
        #   处理剩余未被包含的文件
        rel_other_fnames_without_tags = set(self.get_rel_fname(fname) for fname in other_fnames)
        fnames_already_included = set(rt[0] for rt in ranked_tags)

        top_rank = sorted([(rank, node) for (node, rank) in ranked.items()], reverse=True)
        for rank, fname in top_rank:
            if fname in rel_other_fnames_without_tags:
                rel_other_fnames_without_tags.remove(fname)
            if fname not in fnames_already_included:
                ranked_tags.append((fname,))

        for fname in rel_other_fnames_without_tags:
            ranked_tags.append((fname,))

        return ranked_tags

    def get_ranked_tags_map(
        self,
        chat_fnames,
        other_fnames=None,
        max_map_tokens=None,
        mentioned_fnames=None,
        mentioned_idents=None,
        force_refresh=False,
    ):
        # Create a cache key
        cache_key = [
            tuple(sorted(chat_fnames)) if chat_fnames else None,
            tuple(sorted(other_fnames)) if other_fnames else None,
            max_map_tokens,
        ]

        if self.refresh == "auto":
            cache_key += [
                tuple(sorted(mentioned_fnames)) if mentioned_fnames else None,
                tuple(sorted(mentioned_idents)) if mentioned_idents else None,
            ]
        # 生成唯一缓存键，避免重复计算。排序保证不同输入顺序生成相同键，auto 模式包含更多上下文参数
        cache_key = tuple(cache_key)

        use_cache = False
        if not force_refresh:
            if self.refresh == "manual" and self.last_map:      # 手动模式直接返回上次结果
                return self.last_map

            if self.refresh == "always":                        # 总是重新计算
                use_cache = False
            elif self.refresh == "files":                       # 仅当文件相同时缓存
                use_cache = True
            elif self.refresh == "auto":                        # 根据处理时间决定
                use_cache = self.map_processing_time > 1.0

            # Check if the result is in the cache
            if use_cache and cache_key in self.map_cache:       # 命中缓存
                return self.map_cache[cache_key]

        # If not in cache or force_refresh is True, generate the map
        start_time = time.time()
        result = self.get_ranked_tags_map_uncached(             # 实际计算入口
            chat_fnames, other_fnames, max_map_tokens, mentioned_fnames, mentioned_idents
        )
        end_time = time.time()
        self.map_processing_time = end_time - start_time        # 记录耗时用于auto模式, 记录处理时间用于自适应缓存策略

        # Store the result in the cache
        self.map_cache[cache_key] = result                      # 存储到缓存字典
        self.last_map = result                                  # 更新最后一次结果
        """
        双缓存机制：
            map_cache: 键值对缓存, 支持多组参数组合
            last_map: 快速访问最新结果, 用于manual模式
        """

        return result

    def get_ranked_tags_map_uncached(
        self,
        chat_fnames,
        other_fnames=None,
        max_map_tokens=None,
        mentioned_fnames=None,
        mentioned_idents=None,
    ):
        if not other_fnames:                                # 确保other_fnames是列表
            other_fnames = list()
        if not max_map_tokens:                              # 默认token限制
            max_map_tokens = self.max_map_tokens
        if not mentioned_fnames:                            # 空提及文件集合
            mentioned_fnames = set()
        if not mentioned_idents:                            # 空提及标识符集合
            mentioned_idents = set()

        spin = Spinner(UPDATING_REPO_MAP_MESSAGE)

        ranked_tags = self.get_ranked_tags(
            chat_fnames,
            other_fnames,
            mentioned_fnames,
            mentioned_idents,
            progress=spin.step,                             # 进度反馈：通过Spinner显示处理状态
        )

        other_rel_fnames = sorted(set(self.get_rel_fname(fname) for fname in other_fnames))
        special_fnames = filter_important_files(other_rel_fnames)   # 筛选关键文件
        ranked_tags_fnames = set(tag[0] for tag in ranked_tags)
        special_fnames = [fn for fn in special_fnames if fn not in ranked_tags_fnames]
        special_fnames = [(fn,) for fn in special_fnames]           # 转换为tag格式

        ranked_tags = special_fnames + ranked_tags                  # 合并到结果

        spin.step()

        num_tags = len(ranked_tags)     # 初始范围
        lower_bound = 0                 # 初始范围
        upper_bound = num_tags          # 初始范围
        best_tree = None                # 最优结果记录
        best_tree_tokens = 0            # 最优结果记录

        chat_rel_fnames = set(self.get_rel_fname(fname) for fname in chat_fnames)

        self.tree_cache = dict()

        middle = min(int(max_map_tokens // 25), num_tags)   # 初始中点
        # 通过二分查找平衡内容质量与token限制
        """
        关键逻辑：

            每次迭代生成部分内容的代码树

            允许15%误差范围内提前终止
        """
        while lower_bound <= upper_bound:
            # dump(lower_bound, middle, upper_bound)

            if middle > 1500:
                show_tokens = f"{middle / 1000.0:.1f}K"
            else:
                show_tokens = str(middle)
            spin.step(f"{UPDATING_REPO_MAP_MESSAGE}: {show_tokens} tokens")

            tree = self.to_tree(ranked_tags[:middle], chat_rel_fnames)              # 生成部分树
            num_tokens = self.token_count(tree)                                     # 计算token数

            pct_err = abs(num_tokens - max_map_tokens) / max_map_tokens
            ok_err = 0.15
            if (num_tokens <= max_map_tokens and num_tokens > best_tree_tokens) or pct_err < ok_err:
                best_tree = tree
                best_tree_tokens = num_tokens

                if pct_err < ok_err:
                    break

            if num_tokens < max_map_tokens:
                lower_bound = middle + 1
            else:
                upper_bound = middle - 1        # 查找第一个满足条件的元素

            middle = int((lower_bound + upper_bound) // 2)

        spin.end()
        return best_tree

    tree_cache = dict()

    def render_tree(self, abs_fname, rel_fname, lois):
        mtime = self.get_mtime(abs_fname)
        key = (rel_fname, tuple(sorted(lois)), mtime)

        if key in self.tree_cache:
            return self.tree_cache[key]

        if (
            rel_fname not in self.tree_context_cache
            or self.tree_context_cache[rel_fname]["mtime"] != mtime
        ):
            code = self.io.read_text(abs_fname) or ""
            if not code.endswith("\n"):
                code += "\n"

            context = TreeContext(
                rel_fname,
                code,
                color=False,
                line_number=False,
                child_context=False,
                last_line=False,
                margin=0,
                mark_lois=False,
                loi_pad=0,
                # header_max=30,
                show_top_of_file_parent_scope=False,
            )
            self.tree_context_cache[rel_fname] = {"context": context, "mtime": mtime}

        context = self.tree_context_cache[rel_fname]["context"]
        context.lines_of_interest = set()
        context.add_lines_of_interest(lois)
        context.add_context()
        res = context.format()
        self.tree_cache[key] = res
        return res

    def to_tree(self, tags, chat_rel_fnames):
        if not tags:
            return ""

        cur_fname = None
        cur_abs_fname = None
        lois = None
        output = ""

        # add a bogus tag at the end so we trip the this_fname != cur_fname...
        dummy_tag = (None,)
        for tag in sorted(tags) + [dummy_tag]:
            this_rel_fname = tag[0]
            if this_rel_fname in chat_rel_fnames:
                continue

            # ... here ... to output the final real entry in the list
            if this_rel_fname != cur_fname:
                if lois is not None:
                    output += "\n"
                    output += cur_fname + ":\n"
                    output += self.render_tree(cur_abs_fname, cur_fname, lois)
                    lois = None
                elif cur_fname:
                    output += "\n" + cur_fname + "\n"
                if type(tag) is Tag:
                    lois = []
                    cur_abs_fname = tag.fname
                cur_fname = this_rel_fname

            if lois is not None:
                lois.append(tag.line)

        # truncate long lines, in case we get minified js or something else crazy
        output = "\n".join([line[:100] for line in output.splitlines()]) + "\n"

        return output


def find_src_files(directory):
    if not os.path.isdir(directory):
        return [directory]

    src_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            src_files.append(os.path.join(root, file))
    return src_files


def get_random_color():
    hue = random.random()
    r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(hue, 1, 0.75)]
    res = f"#{r:02x}{g:02x}{b:02x}"
    return res


def get_scm_fname(lang):
    # Load the tags queries
    if USING_TSL_PACK:
        subdir = "tree-sitter-language-pack"
        try:
            path = resources.files(__package__).joinpath(
                "queries",
                subdir,
                f"{lang}-tags.scm",
            )
            if path.exists():
                return path
        except KeyError:
            pass

    # Fall back to tree-sitter-languages
    subdir = "tree-sitter-languages"
    try:
        return resources.files(__package__).joinpath(
            "queries",
            subdir,
            f"{lang}-tags.scm",
        )
    except KeyError:
        return


def get_supported_languages_md():
    from grep_ast.parsers import PARSERS

    res = """
| Language | File extension | Repo map | Linter |
|:--------:|:--------------:|:--------:|:------:|
"""
    data = sorted((lang, ex) for ex, lang in PARSERS.items())

    for lang, ext in data:
        fn = get_scm_fname(lang)
        repo_map = "✓" if Path(fn).exists() else ""
        linter_support = "✓"
        res += f"| {lang:20} | {ext:20} | {repo_map:^8} | {linter_support:^6} |\n"

    res += "\n"

    return res


if __name__ == "__main__":
    fnames = sys.argv[1:]

    chat_fnames = []
    other_fnames = []
    for fname in sys.argv[1:]:
        if Path(fname).is_dir():
            chat_fnames += find_src_files(fname)
        else:
            chat_fnames.append(fname)

    rm = RepoMap(root=".")
    repo_map = rm.get_ranked_tags_map(chat_fnames, other_fnames)

    dump(len(repo_map))
    print(repo_map)
