# Introduction to BRAIN Expression Language

- 튜토리얼: `discover-brain` (Getting Started)
- 페이지 ID: `introduction-brain-expression-language`
- 최종수정: 2025-03-12T05:14:01.490538-04:00
- 분량: PT3M

---

> [HEADING] {"level": "1", "content": "What is Fast Expression?"}

“Fast expression” is a proprietary programming language used by WorldQuant BRAIN that is designed to make it easier to write and test financial models. The language can be thought as a form of pseudo code, which uses natural language and simple programming constructs to convey the logic of the algorithm.

The goal of using “Fast expression” on BRAIN is to provide a clear and concise way to express complex ideas and algorithms that can be easily understood by other developers and researchers. By abstracting away the details of the underlying implementation, it can allow BRAIN users to focus on the high-level logic of their algorithms, rather than getting bogged down in the implementation details.

> [HEADING] {"level": "1", "content": "Characteristics of Fast Expression"}

Just like how an English sentence consists of a subject, verb and object; Fast expression can include data fields, operators and numerical values.

> [HEADING] {"level": "2", "content": "Data fields"}

[Data fields]($reference/datasets) refer to a named collection of data, for example 'open price' or 'close price'.

> [IMAGE] {"title": "Datasets and data fields", "width": 1512, "height": 790, "fileSize": 74162, "url": "https://api.worldquantbrain.com/content/images/IaAoDv9Y8tslj4pCv6M9pFU6TWM=/177/original/dataset_1.png"}

> [HEADING] {"level": "2", "content": "Operators"}

[Operators]($reference/operators) refer to a set of mathematical techniques required to implement your Alpha ideas.

> [IMAGE] {"title": "Operators", "width": 1074, "height": 373, "fileSize": 100643, "url": "https://api.worldquantbrain.com/content/images/0kiNQvb4d7dnltnIbeuU-K7zL_4=/178/original/Operators_1.png"}

> [HEADING] {"level": "1", "content": "Further Knowledge of Fast Expression"}

> [IMAGE] {"title": "Punctuation", "width": 1072, "height": 408, "fileSize": 143837, "url": "https://api.worldquantbrain.com/content/images/Qvkynvp_PtZnsLtCWadN9FkSEZ4=/179/original/Punctuation.png"}

- ***/**** helps to create block comments that span multiple lines of text, while*** */*** denotes the end of the comment. Comments consist of explanatory text to help understand what the code does. [1]
- ***;*** (semicolon) acts as a semicolon in a sentence, separating the end of one sentence from the beginning of another sentence. For the last line of the code (line 13) ; is not needed. [2]
- The last sentence of the entire expression is the Alpha expression that the BRAIN simulator use to calculate the positions to take in each stock. [3]Lastly, Fast expression does not have classes, objects, pointers, or functions.

In summary, Fast expression provides a clear and concise way for users to express complex ideas and algorithms. Don’t worry if you’re not familiar with Fast expression yet. With a bit of practice, we believe you’ll pick it up in no time!
