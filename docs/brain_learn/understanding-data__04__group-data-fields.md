# Group Data Fields 🥈

- 튜토리얼: `understanding-data` (Data)
- 페이지 ID: `group-data-fields`
- 최종수정: 2025-01-08T05:33:05.322350-05:00
- 분량: PT3M

---

> [HEADING] {"level": "1", "content": "Group Data Fields"}

Group Data Fields are fields which have information about instrument segregation into various groups. They are supposed to be used as an input to the group operator. Some grouping type fields are industry, subindustry and sector.

Group Data Fields are available across various datasets. For instance, commonly used groups such as sector, industry, subindustry, and exchange can be located in the [pv1](https://platform.worldquantbrain.com/data/data-sets/pv1) dataset by applying a type == 'group' filter within Data Explorer. In addition, other group data fields can be discovered by browsing all fields and utilizing the same 'group' filter in [Data Explorer](https://platform.worldquantbrain.com/data/data-fields?delay=1&instrumentType=EQUITY&limit=20&offset=0&region=GLB&type=GROUP&universe=TOP3000).

> [HEADING] {"level": "1", "content": "How to utilize Group Data Fields"}

> [HEADING] {"level": "2", "content": "Using group data fields"}

Type of field which has more than one value for every date and instrument. Vector data fields have to be converted into matrix data fields using [vector operators](https://platform.worldquantbrain.com/learn/operators/operators#vector-operators) before using with other operators and matrix data fields. Otherwise, an error message will be returned. Some examples include [pv13](https://platform.worldquantbrain.com/data/data-sets/pv13) and [pv17](https://platform.worldquantbrain.com/data/data-sets/pv17) dataset for USA and ASI.

> [HEADING] {"level": "2", "content": "Creating new groups"}

Apart from using group data, you can also create new group using bucket() operator. This operator creates new groups based on value of any data field.

For example:

asset_group = bucket(rank(assets), range="0.1, 1, 0.1")

This will create a new group consisting of 10 buckets based on rank of assets. Stocks with highest assets will be in top bucket and stocks with lowest assets will be in bottom bucket. You can find the full syntax [here](https://platform.worldquantbrain.com/learn/data-and-operators/detailed-operator-descriptions#bucket)

Finally, you can use group data directly in grouping operator:

group_zscore(<alpha>, <group>)

Do note, it is a good practice to apply densify() operator to groups before using them. Densify operator removes empty groups and improves Alpha performance. You can read more about it [here](https://platform.worldquantbrain.com/learn/data-and-operators/detailed-operator-descriptions#densifyx)

The group operator will hence be written as:

asset_group = bucket(rank(assets), range="0.1, 1, 0.1");
 group_zscore(<alpha>, densify(asset_group))

> [HEADING] {"level": "1", "content": "Group Operators"}

Group operators are a type of cross-sectional operator that compares stocks at a finer level, where the cross-sectional operation is applied within each group, rather than across the entire market. One such example is group_rank vs rank, where in group_rank, the instruments are allocated to their specified group, then within each group, it ranks the instruments based on their input value, as compared to rank, where the ranking is done across all instruments.
