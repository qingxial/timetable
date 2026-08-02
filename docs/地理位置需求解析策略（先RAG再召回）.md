解析地点偏好时，并不是把所有的教室、教学楼都交给大模型，而是先成候选，再在候选里精确匹配
教学楼候选 building_context
教室候选 classroom_context

## 两个候选是怎么来的

### 1. `building_context` 的来源

`building_context(classrooms, text, campus)` 的生成过程：

1. 读取所有教室列表，按教学楼代码 `JXLDM` 去重，得到教学楼候选。
2. 如果有 `campus` 校区信息，会优先只保留该校区内的教学楼。
3. 从当前排课要求文本 `text` 中提取位置关键词（如“主楼B”“西2楼”“B-103”等）。
4. 如果文本中有位置词，则进一步匹配这些关键词与教学楼名称/代码，优先把匹配到的教学楼排在前面。
5. 最终把最多 `MAX_BUILDINGS_IN_PROMPT` 条教学楼候选格式化成：
   - `- 教学楼名称 | 教学楼代码 | 校区 | 别名`

所以 `building_context` 其实是从“教室资源表”里提取教学楼信息，并结合当前文本和校区做过滤、排序。

---

### 2. `classroom_context` 的来源

`classroom_context(classrooms, text, campus, capacity)` 的生成过程：

1. 先调用 `filter_by_course_context(classrooms, campus, capacity)`：
   - 如果有校区 `campus`，优先筛选同校区教室；
   - 如果有容量 `capacity`，优先筛选座位数大于等于该容量的教室；
   - 只保留允许排课的教室（`SFYXPK` 为 `1` 或空）。
2. 再从文本 `text` 中提取位置关键词。
3. 如果文本里有位置关键词，则用 `text_matches_room(room, tokens)` 匹配教室/教学楼/教室代码等字段：
   - 匹配字段包括 `JASMC`、`JXLMC`、`JASDM`、`JXLDM`
   - 还会去掉空格、连字符、中文括号等，进行“压缩后字符串匹配”
4. 如果有匹配到的教室，就把这些匹配结果放到前面，并与前面筛选出的“基础列表”合并去重；
   如果没有匹配到，则直接使用过滤后的基础列表。
5. 最后把最多 `MAX_CLASSROOMS_IN_PROMPT` 条记录格式化成：
   - `- 教室名称 | 教室代码 | 教学楼名称 | 教学楼代码 | 校区 | 容量 | 类型`

---

### 关键函数

- `read_classrooms(path)`：从教室资源 Excel 读取并构造 `Classroom` 对象
- `filter_by_course_context(...)`：按校区、容量、是否允许排课过滤
- `extract_location_tokens(text)`：从文本中抓取可能的建筑/教室关键词
- `text_matches_room(room, tokens)`：判断关键词是否匹配某个教室/教学楼
- `building_context(...)`：生成教学楼候选文本
- `classroom_context(...)`：生成教室候选文本

---

### 简单结论

这两个候选都是从 “教室资源表” 里来的，先读出所有教室数据，再结合当前行的校区、容量和文本里出现的位置词来筛选和排序，最后格式化成 prompt 里要放的候选列表。

### 要不要加embedding 相似度检索呢。。
### 时间和地点与往年相同这种需求怎么解决